"""Mission sampling — prompt builders for the advisory LLM path.

The Mission engine routes optional model-driven advice
(Strategy_Revision rationales / next-strategy proposals on ``adjust``,
Final_Report ``lessons`` / ``recommended_followups`` on ``complete`` and
``terminate``) through a small, transport-agnostic plumbing pipe that
starts here. This module is the **prompt-assembly half** of that pipe:
pure Python, sync, no MCP / boto3 / fastmcp imports. Backends, capability
detection, response validation, and orchestration helpers land in sibling
sections of this file in subsequent commits.

The two render methods on :class:`SamplingPrompt` produce a
deterministic ``str`` payload from the bare data the caller passes in:

* :meth:`SamplingPrompt.assemble` — the Strategy_Revision prompt. Includes
  the directive, the Success_Criteria with current per-criterion status,
  the resolved Tool_Allowlist with each tool's docstring, an explicit
  budget context block, the last five Iteration summaries (Observation
  fields larger than :data:`OBSERVATION_FIELD_BYTE_CAP` truncated to
  :data:`OBSERVATION_FIELD_TRUNCATE_TO` bytes plus the marker
  :data:`TRUNCATION_MARKER`, with the original byte lengths recorded
  under the ``_original_bytes`` map), and the JSON Schema instruction
  block built around :data:`STRATEGY_REVISION_SCHEMA`.
* :meth:`SamplingPrompt.assemble_final_lessons` — the Final_Report
  prompt. Reuses the directive / criteria assembly but emits
  :data:`FINAL_LESSONS_SCHEMA` instead of the strategy-revision schema,
  and replaces the iteration-by-iteration Observation summaries with a
  short ``verdict`` / ``verdict_reason`` summary list because the
  Final_Report path does not need raw Observation history.

Both render methods cap total output at :data:`PROMPT_BYTE_BUDGET`
bytes (UTF-8). When the assembled prompt exceeds the cap, the oldest
Iteration summary is dropped and the prompt re-rendered, repeating
until the prompt fits. Truncation and dropping are deterministic — the
same inputs always produce a byte-identical output. This is the
property the tests under
``tests/test_mission_sampling.py`` pin down.
"""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, cast, runtime_checkable

from gco.bedrock import (
    BEDROCK_READ_TIMEOUT_SECONDS,
    BedrockResponseTruncatedError,
    build_bedrock_converse_options,
    extract_bedrock_converse_text,
    get_default_mission_model_id,
    raise_if_bedrock_ftu_form_error,
)

from . import validation as _validation
from .types import Criterion, CriterionResult, IterationRecord, Observation, Strategy
from .validation import MissionValidationError

# <pyflowchart-code-diagram> BEGIN - auto-inserted, do not edit
# Generated at (UTC): 2026-08-31T15:58:29Z
# Generated from Git commit: 614edab7df2577751b831babdd4d6acf7d83a43d
# Flowchart(s) generated from this file:
#   * ``maybe_sample_strategy_revision`` -> ``diagrams/code_diagrams/gco_mcp/mission/sampling.maybe_sample_strategy_revision.html``
#     (PNG: ``diagrams/code_diagrams/gco_mcp/mission/sampling.maybe_sample_strategy_revision.png``)
# Regenerate with ``SOURCE_DATE_EPOCH=<unix-seconds> GCO_DIAGRAM_SOURCE_COMMIT=<40-char-sha> python diagrams/generate.py --code-only``.
# <pyflowchart-code-diagram> END


__all__ = [
    "BEDROCK_READ_TIMEOUT_SECONDS",
    "BEDROCK_TEMPERATURE",
    "DEFAULT_BEDROCK_REGION",
    "ENV_BEDROCK_MODEL_ID",
    "ENV_BEDROCK_REGION",
    "ENVIRONMENT_CONTEXT_BYTE_CAP",
    "FINAL_LESSONS_SCHEMA",
    "BedrockSamplingBackend",
    "MissionValidationError",
    "OBSERVATION_FIELD_BYTE_CAP",
    "OBSERVATION_FIELD_TRUNCATE_TO",
    "PRIOR_MISSIONS_BYTE_CAP",
    "PROMPT_BYTE_BUDGET",
    "RECENT_ITERATIONS_LIMIT",
    "STRATEGY_REVISION_SCHEMA",
    "STRATEGY_SHAPE_SCHEMA",
    "SamplingBackend",
    "SamplingFallback",
    "SamplingPrompt",
    "SamplingTransportError",
    "SamplingUsed",
    "TRUNCATION_MARKER",
    "maybe_sample_final_lessons",
    "maybe_sample_strategy_revision",
    "resolve_sampling_state",
    "select_sampling_backend",
    "validate_strategy_against_catalog",
]


# ---------------------------------------------------------------------------
# Tunables (named so tests can reference them without hard-coding magic)
# ---------------------------------------------------------------------------

#: Per-Observation-field byte cap. Fields whose JSON-serialised UTF-8
#: byte length exceeds this value are truncated. A field whose byte
#: length is exactly equal to this value is **not** truncated — the
#: comparison uses strict greater-than to keep the boundary stable.
OBSERVATION_FIELD_BYTE_CAP: int = 4096

#: Target byte length after truncation. The truncated string is the
#: first ``OBSERVATION_FIELD_TRUNCATE_TO`` bytes of the JSON-serialised
#: form, decoded with ``errors="ignore"`` so a multi-byte boundary in
#: the middle of a UTF-8 codepoint cannot raise, with
#: :data:`TRUNCATION_MARKER` appended.
OBSERVATION_FIELD_TRUNCATE_TO: int = 2048

#: Marker appended to every truncated field so the reader can see at a
#: glance the field was clipped.
TRUNCATION_MARKER: str = "... [truncated]"

#: Total prompt byte budget. The render methods drop the oldest
#: Iteration summary one at a time until ``len(prompt.encode("utf-8"))
#: <= PROMPT_BYTE_BUDGET``.
PROMPT_BYTE_BUDGET: int = 32768

#: Maximum number of Iteration summaries to include even if the byte
#: budget is plentiful. The caller is expected to pass at most this
#: many already; the builder slices defensively.
RECENT_ITERATIONS_LIMIT: int = 5

#: Per-Environment-context byte cap. The optional environment context
#: block (``=== Environment context ===``) is its own truncation
#: domain so the section can never grow without bound and push the
#: rest of the prompt over :data:`PROMPT_BYTE_BUDGET`. The cap mirrors
#: :data:`OBSERVATION_FIELD_BYTE_CAP` because both surfaces hold the
#: same flavour of structured live signal (cluster + queue snapshots
#: in this case) and the same truncation marker convention applies.
ENVIRONMENT_CONTEXT_BYTE_CAP: int = 4096

#: Byte cap for the optional prior-missions block (``=== Prior similar
#: missions ===``). Its own truncation domain for the same reason as
#: :data:`ENVIRONMENT_CONTEXT_BYTE_CAP`: retrieved lessons are
#: free-text of unbounded length and must never crowd the rest of the
#: prompt out of :data:`PROMPT_BYTE_BUDGET`.
PRIOR_MISSIONS_BYTE_CAP: int = 4096

#: The memory-item fields the prior-missions block passes through to
#: the prompt — the vector index's ``INCLUDE`` projection plus the key
#: and the similarity score. Anything else a future projection might
#: surface is dropped so the block's shape stays stable.
_PRIOR_MISSION_FIELDS: frozenset[str] = frozenset(
    {
        "session_id",
        "directive",
        "lessons",
        "recommended_followups",
        "final_verdict",
        "verdict_reason",
        "iteration_count",
        "completed_at",
        "score",
    }
)


# ---------------------------------------------------------------------------
# Environment context summarisation
# ---------------------------------------------------------------------------


def _summarise_environment_context(env: Mapping[str, Any]) -> dict[str, Any]:
    """Return a JSON-safe, byte-capped summary of the environment context.

    The block is rendered into the Strategy_Revision prompt under
    ``=== Environment context ===``. It carries small, slow-moving
    live signals — per-region queue depths, GPU utilisation, deployed
    region list, reservation counts — that the model would otherwise
    have to spend tool calls to discover.

    Two guarantees on the output:

    1. The serialised form fits inside :data:`ENVIRONMENT_CONTEXT_BYTE_CAP`
       UTF-8 bytes. When the input does not, top-level fields are
       evaluated in sorted-key order, dropped one at a time from the
       largest contributor down, and the dropped key list is recorded
       under ``"_dropped_fields"`` so the operator can spot which
       inputs got pruned.
    2. Top-level keys are emitted in sorted order so two callers
       passing semantically-identical dicts produce a byte-identical
       block — the same property the determinism tests pin down for
       Observation summaries.
    """
    # Defensive copy + sort so insertion order doesn't leak.
    ordered: dict[str, Any] = {key: env[key] for key in sorted(env.keys())}
    serialised = _dumps(ordered)
    if _utf8_len(serialised) <= ENVIRONMENT_CONTEXT_BYTE_CAP:
        return ordered

    # Drop largest top-level field first, repeating until under cap.
    # Records dropped keys so the operator (and the audit pipeline)
    # can see what got pruned without having to diff against the
    # gather helper's output.
    dropped: list[str] = []
    working = dict(ordered)
    while _utf8_len(_dumps(working)) > ENVIRONMENT_CONTEXT_BYTE_CAP and working:
        biggest_key = max(working, key=lambda k: _utf8_len(_dumps(working[k])))
        dropped.append(biggest_key)
        del working[biggest_key]

    if dropped:
        # Sort the dropped list so its position in the prompt is stable
        # regardless of which key happened to be biggest first.
        working["_dropped_fields"] = sorted(dropped)
    return working


# ---------------------------------------------------------------------------
# Prior-missions summarisation
# ---------------------------------------------------------------------------


def _summarise_prior_missions(
    missions: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Return a JSON-safe, byte-capped summary of retrieved prior missions.

    Rendered into the prompt under ``=== Prior similar missions ===``.
    The input is the :meth:`mcp.mission.memory.MissionMemoryStore.search_similar`
    result list, ordered most-similar-first.

    Three guarantees on the output:

    1. Only the fields in :data:`_PRIOR_MISSION_FIELDS` pass through,
       emitted in sorted-key order — so two semantically-identical
       inputs produce a byte-identical block (the determinism property
       every prompt section pins down), and a recreated index with a
       wider projection cannot change the block's shape.
    2. Each mission's ``lessons`` field is truncated to
       :data:`OBSERVATION_FIELD_TRUNCATE_TO` bytes with
       :data:`TRUNCATION_MARKER` when it exceeds
       :data:`OBSERVATION_FIELD_BYTE_CAP` — one verbose write-up must
       not evict every other retrieved mission.
    3. The serialised list fits in :data:`PRIOR_MISSIONS_BYTE_CAP`
       UTF-8 bytes. When it does not, the *least similar* mission (the
       list tail) is dropped first, repeating until under cap.
    """
    summarised: list[dict[str, Any]] = []
    for mission in missions:
        entry = {key: mission[key] for key in sorted(_PRIOR_MISSION_FIELDS) if key in mission}
        lessons = entry.get("lessons")
        if isinstance(lessons, str) and _utf8_len(lessons) > OBSERVATION_FIELD_BYTE_CAP:
            entry["lessons"] = _truncate_serialised(lessons)
        summarised.append(entry)

    while _utf8_len(_dumps(summarised)) > PRIOR_MISSIONS_BYTE_CAP and summarised:
        summarised.pop()
    return summarised


# ---------------------------------------------------------------------------
# Bedrock backend tunables
# ---------------------------------------------------------------------------

#: The default Bedrock model identifier is read on demand from ``cdk.json``
#: ``context.bedrock.mission_default_model_id`` through the lightweight
#: :func:`gco.bedrock.get_default_mission_model_id` resolver, so unrelated
#: imports never couple to Bedrock configuration resolution.
#:
#: Operators with regulatory or model-governance requirements can override per
#: call via ``GCO_MISSION_BEDROCK_MODEL_ID`` or ``--bedrock-model-id``; see
#: docs/CUSTOMIZATION.md ("Bedrock Model Selection").

#: Default Bedrock region. The capacity advisor pins ``us-east-1`` for
#: the same reason: cross-region inference profiles routinely surface
#: in ``us-east-1`` first and our installations have it whitelisted.
DEFAULT_BEDROCK_REGION: str = "us-east-1"

#: Env var that overrides the canonical Mission model default at runtime.
ENV_BEDROCK_MODEL_ID: str = "GCO_MISSION_BEDROCK_MODEL_ID"

#: Env var that overrides :data:`DEFAULT_BEDROCK_REGION` at runtime.
ENV_BEDROCK_REGION: str = "GCO_MISSION_BEDROCK_REGION"

#: Sampling temperature for non-default Bedrock model overrides. The
#: canonical Nova 2 default uses high reasoning, for which AWS requires
#: temperature to be unset; :func:`build_bedrock_converse_options` removes it.
BEDROCK_TEMPERATURE: float = 0.2


# ---------------------------------------------------------------------------
# JSON Schemas — embedded as module-level constants
# ---------------------------------------------------------------------------

# The Strategy shape mirrors the ``Strategy`` TypedDict from
# ``gco_mcp/mission/types.py``: every key is optional in isolation and the
# validator enforces the mutual-exclusivity invariant (exactly one of
# ``tool_calls`` or ``script`` populated). The schema below mirrors
# that with a ``oneOf`` clause. The model-side validator in subsequent
# commits performs the same check on parsed responses; this schema is
# the textual instruction the prompt embeds for the model.
STRATEGY_SHAPE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "tool_calls": {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "object",
                "additionalProperties": True,
                "required": ["tool_name", "args"],
                "properties": {
                    "tool_name": {"type": "string", "minLength": 1},
                    "args": {"type": "object"},
                },
            },
        },
        "script": {"type": "string", "minLength": 1},
        "expected_observation_keys": {
            "type": "array",
            "items": {"type": "string"},
        },
        "rationale": {"type": "string"},
    },
    "oneOf": [
        {"required": ["tool_calls"]},
        {"required": ["script"]},
    ],
}

#: JSON Schema for the model's response when called for a
#: Strategy_Revision. The model must return exactly these three keys.
STRATEGY_REVISION_SCHEMA: dict[str, Any] = {
    "$schema": "http://json-schema.org/draft-07/schema#",
    "title": "Mission strategy revision",
    "type": "object",
    "additionalProperties": False,
    "required": ["revision_rationale", "next_strategy", "confidence"],
    "properties": {
        "revision_rationale": {"type": "string", "minLength": 1},
        "next_strategy": STRATEGY_SHAPE_SCHEMA,
        "confidence": {
            "type": "number",
            "minimum": 0.0,
            "maximum": 1.0,
        },
    },
}

#: JSON Schema for the model's response when called from the
#: Final_Report writer.
FINAL_LESSONS_SCHEMA: dict[str, Any] = {
    "$schema": "http://json-schema.org/draft-07/schema#",
    "title": "Mission final lessons",
    "type": "object",
    "additionalProperties": False,
    "required": ["lessons", "recommended_followups"],
    "properties": {
        "lessons": {
            "type": "array",
            "minItems": 1,
            "items": {"type": "string", "minLength": 1},
        },
        "recommended_followups": {
            "type": "array",
            "items": {"type": "string", "minLength": 1},
        },
    },
}


# ---------------------------------------------------------------------------
# JSON helpers — every dump in this module routes through ``_dumps``
# so the byte-counting and the rendered prompt agree on the encoding.
# ---------------------------------------------------------------------------


def _dumps(value: Any, *, indent: int | None = None) -> str:
    """Deterministic JSON encoder used everywhere in this module.

    ``sort_keys=True`` is the source of determinism — Python dicts are
    insertion-ordered, but the Hypothesis strategies that drive the
    determinism tests build dicts via ``fixed_dictionaries`` whose
    insertion order is implementation-defined, so sorting is the only
    way to get byte-identical output across two draws of the same
    abstract dict shape. ``ensure_ascii=False`` keeps non-ASCII text
    intact so the byte-budget bookkeeping matches what the LLM sees.
    """
    return json.dumps(
        value,
        sort_keys=True,
        ensure_ascii=False,
        indent=indent,
        separators=(",", ": ") if indent is not None else (",", ":"),
    )


def _utf8_len(s: str) -> int:
    """UTF-8 byte length of ``s`` — the only "size" the budget cares about."""
    return len(s.encode("utf-8"))


def _truncate_serialised(serialised: str) -> str:
    """Slice a serialised value down to ``OBSERVATION_FIELD_TRUNCATE_TO``
    bytes plus :data:`TRUNCATION_MARKER`. Decode-safe.

    The slicing is byte-level rather than codepoint-level because the
    cap itself is a byte budget. Using ``errors="ignore"`` strips any
    partial codepoint at the boundary so the result is always valid
    UTF-8 — at the cost of dropping at most three bytes' worth of an
    incomplete codepoint, which is acceptable for an advisory summary.
    """
    truncated_bytes = serialised.encode("utf-8")[:OBSERVATION_FIELD_TRUNCATE_TO]
    truncated_str = truncated_bytes.decode("utf-8", errors="ignore")
    return truncated_str + TRUNCATION_MARKER


# ---------------------------------------------------------------------------
# Observation summarisation
# ---------------------------------------------------------------------------


def _summarise_observation(obs: Mapping[str, Any] | Observation) -> dict[str, Any]:
    """Return a JSON-safe summary of an Observation with oversized fields
    truncated and the original byte lengths recorded.

    The summary mirrors the Observation's top-level keys. For each key
    whose JSON-serialised value exceeds :data:`OBSERVATION_FIELD_BYTE_CAP`
    bytes, the value is replaced by the byte-clamped + marker string and
    the original byte length is recorded under
    ``summary["_original_bytes"][<key>]``. Fields at or below the cap pass
    through unchanged.

    The ``_original_bytes`` private key is omitted entirely when no field
    was truncated so the summary stays clean for the common case.
    """
    obs_map: Mapping[str, Any] = cast("Mapping[str, Any]", obs)
    summary: dict[str, Any] = {}
    original_bytes: dict[str, int] = {}
    # Sorting the keys guarantees the rendered prompt is byte-identical
    # even when the caller's dict was built in a different insertion
    # order than another caller's identical-shape dict.
    for key in sorted(obs_map.keys()):
        if key == "_original_bytes":
            # A defensively-guarded passthrough: a previous summarisation
            # round (e.g., a re-render after dropping iterations) must
            # not double-count the marker map.
            continue
        value = obs_map[key]
        serialised = _dumps(value)
        n_bytes = _utf8_len(serialised)
        if n_bytes > OBSERVATION_FIELD_BYTE_CAP:
            summary[key] = _truncate_serialised(serialised)
            original_bytes[key] = n_bytes
        else:
            summary[key] = value
    if original_bytes:
        summary["_original_bytes"] = original_bytes
    return summary


def _summarise_iteration(iteration: Mapping[str, Any] | IterationRecord) -> dict[str, Any]:
    """Build the per-iteration summary that feeds the Strategy_Revision prompt.

    The summary keeps just the fields a downstream model needs to
    reason about: the iteration index, the strategy that was tried,
    the verdict + reason, and the size-capped Observation. Phase
    timestamps and the criteria-evaluation list are intentionally
    omitted because a) they are deterministic functions of fields the
    model already sees in the criteria-status block, and b) keeping
    them out shrinks the per-iteration footprint so the byte budget
    holds with five iterations more often.
    """
    obs = iteration.get("observation") or {}
    return {
        "iteration_index": iteration.get("iteration_index"),
        "strategy": iteration.get("strategy") or {},
        "verdict": iteration.get("verdict"),
        "verdict_reason": iteration.get("verdict_reason"),
        "observation_summary": _summarise_observation(obs),
    }


def _summarise_iteration_for_lessons(
    iteration: Mapping[str, Any] | IterationRecord,
) -> dict[str, Any]:
    """Final_Report summary — verdict + reason only, no Observation.

    The Final_Report path needs to reason about *what happened* across
    the run, not the per-iteration tool output. Dropping the Observation
    keeps the prompt small enough that the byte budget never bites in
    practice for sessions of any reasonable length.
    """
    return {
        "iteration_index": iteration.get("iteration_index"),
        "verdict": iteration.get("verdict"),
        "verdict_reason": iteration.get("verdict_reason"),
    }


# ---------------------------------------------------------------------------
# Criteria status pairing
# ---------------------------------------------------------------------------


def _pair_criteria_with_status(
    criteria: Sequence[Criterion],
    statuses: Sequence[CriterionResult],
) -> list[dict[str, Any]]:
    """Return ``criteria`` annotated with their most recent status entry.

    Each entry in the result mirrors the Criterion definition (the kind,
    the required-flag, the kind-specific payload keys) and adds a
    nested ``status`` block populated from the matching ``CriterionResult``
    by ``criterion_id``. Criteria with no matching status entry get
    ``status`` set to ``{"status": "inconclusive", "evidence": null}``
    so the model always sees a stable shape.

    The ``_parsed_ast`` private key on a ``predicate`` criterion is
    stripped — it is a Python ``ast.Expression`` object that is not
    JSON-serialisable and that the model has no use for.
    """
    by_id: dict[str, CriterionResult] = {}
    for s in statuses:
        cid = s.get("criterion_id")
        if cid is None:
            continue
        # If the caller passes duplicates (older then newer), prefer the
        # last entry — that's the most-recent-wins convention the engine
        # uses everywhere else.
        by_id[cid] = s

    out: list[dict[str, Any]] = []
    for c in criteria:
        cid = c.get("criterion_id")
        # Strip private cached AST and surface only the prompt-relevant fields.
        public = {k: v for k, v in c.items() if not k.startswith("_")}
        match = by_id.get(cid) if cid is not None else None
        if match is None:
            public["status"] = {
                "status": "inconclusive",
                "evidence": None,
            }
        else:
            public["status"] = {
                "status": match.get("status"),
                "evidence": match.get("evidence"),
                "evaluated_at": match.get("evaluated_at"),
            }
        out.append(public)
    return out


# ---------------------------------------------------------------------------
# Tool allowlist rendering
# ---------------------------------------------------------------------------


def _render_tool_allowlist(
    allowlist: Sequence[str],
    docstrings: Mapping[str, str],
    schemas: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Pair every allowlisted tool name with its docstring and input schema.

    Tools without a registered docstring get an empty string — the
    prompt remains valid; the model just sees a tool name with no
    inline description. Tools without a schema get ``null`` so the
    model knows no args are required. Names are emitted in the
    caller's allowlist order so the prompt is identical for two
    callers that pass the same list.
    """
    rendered: list[dict[str, Any]] = []
    for name in allowlist:
        entry: dict[str, Any] = {
            "tool_name": name,
            "docstring": str(docstrings.get(name, "")),
        }
        if schemas:
            schema = schemas.get(name)
            if schema is not None:
                entry["input_schema"] = schema
        rendered.append(entry)
    return rendered


# ---------------------------------------------------------------------------
# Budget context rendering
# ---------------------------------------------------------------------------


def _render_budget_context(
    *,
    remaining_iterations: int,
    remaining_wall_clock_secs: float | None,
    allow_scripts: bool,
) -> dict[str, Any]:
    """Render the budget context block. Stable shape regardless of inputs.

    ``None`` for the wall-clock cap is rendered verbatim as JSON
    ``null`` so the model can disambiguate "unbounded" from "0".
    """
    return {
        "remaining_iterations": int(remaining_iterations),
        "remaining_wall_clock_seconds": (
            float(remaining_wall_clock_secs) if remaining_wall_clock_secs is not None else None
        ),
        "allow_scripted_strategies": bool(allow_scripts),
    }


# ---------------------------------------------------------------------------
# SamplingPrompt — the public class
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SamplingPrompt:
    """Bundle the bare data needed to assemble a sampling prompt string.

    The dataclass is ``frozen=True`` so callers cannot mutate the inputs
    between an :meth:`assemble` call and an :meth:`assemble_final_lessons`
    call — both methods produce deterministic outputs from the same
    bound state, which is the property the determinism tests pin down.

    All inputs are required positionally or by keyword; defaults are
    only provided where the design spec defines a default.
    """

    directive: str
    success_criteria: Sequence[Criterion]
    criteria_status: Sequence[CriterionResult]
    recent_iterations: Sequence[IterationRecord]
    tool_allowlist: Sequence[str]
    tool_docstrings: Mapping[str, str]
    remaining_iterations: int
    remaining_wall_clock_secs: float | None
    allow_scripts: bool = field(default=False)
    #: Per-tool JSON Schema for the input parameters. Keyed by tool
    #: name; values are the JSON-serialisable schema dict (or ``None``
    #: for tools that take no args). Included in the prompt so the
    #: Strategy_Revision model can propose valid ``args`` dicts.
    tool_schemas: Mapping[str, Any] = field(default_factory=dict)
    #: Optional snapshot of slow-moving live signals (per-region queue
    #: depth, GPU utilisation, deployed-region list, reservation
    #: counts, etc.) gathered once at session start and reused on
    #: every iteration's prompt. ``None`` (the default) suppresses the
    #: ``=== Environment context ===`` section entirely so the prompt
    #: stays byte-identical to the pre-environment-context shape —
    #: that's what every existing determinism test pins down.
    environment_context: Mapping[str, Any] | None = field(default=None)
    #: Optional list of similar past missions retrieved from the
    #: mission-memory vector index (most-similar-first), gathered once
    #: per engine wiring and reused on every iteration's prompt.
    #: ``None`` (the default) suppresses the ``=== Prior similar
    #: missions ===`` section entirely — the same byte-identical
    #: contract as :attr:`environment_context`, and what keeps every
    #: pre-memory prompt (and the determinism suite) unchanged.
    prior_missions: Sequence[Mapping[str, Any]] | None = field(default=None)

    # ---- Strategy_Revision rendering --------------------------------------

    def assemble(self) -> str:
        """Return the Strategy_Revision prompt string.

        The output is capped at :data:`PROMPT_BYTE_BUDGET` UTF-8 bytes.
        When the freshly-assembled prompt exceeds the cap, the oldest
        Iteration summary is dropped and the prompt re-rendered. The
        loop terminates because each drop monotonically shrinks the
        prompt and there is a non-iteration baseline that fits well
        under the cap on its own (the directive, criteria, allowlist,
        budget block, and schema instruction together are ~6-10 KB
        for any reasonable session shape).
        """
        # Defensive slice — the caller is asked to pass at most five,
        # but if they pass more, take the most recent five.
        iterations: list[IterationRecord] = list(self.recent_iterations[-RECENT_ITERATIONS_LIMIT:])

        while True:
            text = self._render(
                schema=STRATEGY_REVISION_SCHEMA,
                iterations=[_summarise_iteration(it) for it in iterations],
                schema_purpose="strategy_revision",
            )
            if _utf8_len(text) <= PROMPT_BYTE_BUDGET or not iterations:
                return text
            # Drop the oldest iteration and try again.
            iterations = iterations[1:]

    # ---- Final_Report rendering -------------------------------------------

    def assemble_final_lessons(self) -> str:
        """Return the Final_Report ``lessons`` prompt string.

        The shape parallels :meth:`assemble` but emits
        :data:`FINAL_LESSONS_SCHEMA` and uses iteration **verdict
        summaries only** instead of full Observation summaries. The same
        :data:`PROMPT_BYTE_BUDGET` byte cap applies; the same
        oldest-first drop policy kicks in if the cap is exceeded.
        """
        iterations: list[IterationRecord] = list(self.recent_iterations)

        while True:
            text = self._render(
                schema=FINAL_LESSONS_SCHEMA,
                iterations=[_summarise_iteration_for_lessons(it) for it in iterations],
                schema_purpose="final_lessons",
            )
            if _utf8_len(text) <= PROMPT_BYTE_BUDGET or not iterations:
                return text
            iterations = iterations[1:]

    # ---- Internal renderer ------------------------------------------------

    def _render(
        self,
        *,
        schema: dict[str, Any],
        iterations: Sequence[Mapping[str, Any]],
        schema_purpose: str,
    ) -> str:
        """Format the full prompt from the section blocks.

        The text layout is fixed — every section is delimited by a
        ``=== <name> ===`` header so the model can latch onto a
        predictable structure. Section bodies are JSON wherever the
        content is structured; the directive itself is rendered as
        free text because that is how the operator wrote it.
        """
        criteria_block = _pair_criteria_with_status(self.success_criteria, self.criteria_status)
        tool_block = _render_tool_allowlist(
            self.tool_allowlist, self.tool_docstrings, self.tool_schemas
        )
        budget_block = _render_budget_context(
            remaining_iterations=self.remaining_iterations,
            remaining_wall_clock_secs=self.remaining_wall_clock_secs,
            allow_scripts=self.allow_scripts,
        )

        if schema_purpose == "strategy_revision":
            preamble = (
                "You are advising a Mission goal-directed iteration loop. "
                "Propose the next Strategy that moves the Mission toward "
                "satisfying its Success_Criteria. The Verdict label, "
                "budget enforcement, and Criteria evaluation are all "
                "computed server-side and are unaffected by your output. "
                "Your role is advisory: the rationale and next_strategy "
                "you produce are validated against the Tool_Allowlist and "
                "the remaining budget before being adopted.\n\n"
                "IMPORTANT: You may propose MULTIPLE tool calls in a "
                "single iteration by including multiple entries in the "
                "tool_calls array. This is especially useful when the "
                "unmet criteria require results from different tools — "
                "calling them all in one iteration lets the evaluator "
                "see all results together. Use the input_schema in the "
                "Tool allowlist section to construct valid args for each "
                "tool call."
            )
            recent_header = "Recent iterations (oldest first)"
        else:
            preamble = (
                "You are advising a Mission goal-directed iteration loop "
                "that has just reached a terminal Verdict. Produce the "
                "lessons learned and the recommended follow-ups for the "
                "operator. Your output is merged into the Final_Report; "
                "the Verdict label, budget bookkeeping, and Criteria "
                "evaluation that produced the terminal state are "
                "deterministic server-side outputs and are not under "
                "review."
            )
            recent_header = "Iteration verdict summary (oldest first)"

        sections: list[str] = []
        sections.append(preamble)
        sections.append("")
        sections.append("=== Mission directive ===")
        sections.append(self.directive)
        sections.append("")
        sections.append("=== Success criteria with current status ===")
        sections.append(_dumps(criteria_block, indent=2))
        sections.append("")
        sections.append("=== Tool allowlist ===")
        sections.append(_dumps(tool_block, indent=2))
        sections.append("")
        sections.append("=== Budget context ===")
        sections.append(_dumps(budget_block, indent=2))
        sections.append("")
        if self.environment_context is not None:
            # Truncated + key-sorted — see :func:`_summarise_environment_context`.
            # Emitting a header even for an empty dict means a session
            # that opted in but had a probe failure still surfaces
            # "we tried" so the operator can act on the gap.
            env_summary = _summarise_environment_context(self.environment_context)
            sections.append("=== Environment context (slow-moving live signals) ===")
            sections.append(_dumps(env_summary, indent=2))
            sections.append("")
        if self.prior_missions is not None:
            # Institutional memory: the closest past missions by directive
            # similarity, with their lessons and verdicts. Advisory only —
            # summarised and byte-capped in its own truncation domain.
            sections.append("=== Prior similar missions (institutional memory) ===")
            sections.append(
                "Lessons and outcomes from the most similar past missions, "
                "most similar first. Treat them as advisory context: they "
                "may suggest which tools or query shapes worked before, or "
                "what to avoid repeating."
            )
            sections.append(_dumps(_summarise_prior_missions(self.prior_missions), indent=2))
            sections.append("")
        sections.append(f"=== {recent_header} ===")
        sections.append(_dumps(list(iterations), indent=2))
        sections.append("")
        sections.append("=== Output schema ===")
        sections.append(
            "Respond with a single JSON object that validates against "
            "the JSON Schema below. Do not include any prose outside "
            "the JSON object."
        )
        sections.append(_dumps(schema, indent=2))

        return "\n".join(sections)


# ---------------------------------------------------------------------------
# Backend protocol and transport-error type
# ---------------------------------------------------------------------------


@runtime_checkable
class SamplingBackend(Protocol):
    """Transport-agnostic surface for the advisory LLM call.

    Implementations bind a concrete transport (e.g., the MCP
    ``Context.sample`` capability or ``bedrock-runtime:Converse``) and
    expose a single async ``sample`` entry point. The protocol is
    ``runtime_checkable`` so call sites — and tests — can use
    ``isinstance(backend, SamplingBackend)`` to gate dispatch on a
    duck-typed backend instance.

    Attributes:
        backend_name: Stable identifier the audit pipeline emits in the
            ``sampling_backend`` field. Bedrock is the only transport
            the system supports: MCP client sampling (``ctx.sample``)
            left the protocol with FastMCP 4's sessionless era, so
            missions sample server-side regardless of how they were
            started.
        model_id: The concrete model identifier the backend will route
            the prompt to. Echoed in audit events so replay can
            reproduce the exact request.
    """

    backend_name: Literal["bedrock"]
    model_id: str

    async def sample(self, prompt: SamplingPrompt) -> str:
        """Render ``prompt`` through the bound transport and return the
        raw model output text. Implementations raise
        :class:`SamplingTransportError` (with a transport-tagged
        ``code``) on any transport-layer failure so the engine's
        fallback policy can branch on a single, well-typed exception.
        """
        ...


class SamplingTransportError(Exception):
    """Transport-layer failure raised by a :class:`SamplingBackend`.

    The mandatory ``code`` attribute tags the failure class so the
    engine's deterministic-fallback path can branch on a stable string
    without parsing the message. The convention is
    ``"<backend>_<error_class>"`` for backend-specific failures and a
    short, snake-cased label for backend-agnostic failures.

    Documented codes (used elsewhere in the Mission stack):

    * ``"bedrock_AccessDeniedException"`` — IAM denied
      ``bedrock:InvokeModel`` for the resolved model.
    * ``"bedrock_malformed_response"`` — Converse returned a payload
      that did not have the expected ``output.message.content[0].text``
      shape.
    * ``"bedrock_truncated_response"`` — the answer was cut off by an
      output-token limit (``stopReason == "max_tokens"``), so its text
      cannot be trusted to be complete.
    * ``"bedrock_no_credentials"`` — the local ``boto3`` session could
      not resolve credentials.

    Args:
        code: Mandatory failure tag (see examples above).
        message: Optional human-readable detail. When present, it is
            joined to ``code`` with ``": "`` for the string
            representation; when absent, ``str(self)`` is just the
            ``code``.
    """

    def __init__(self, code: str, message: str | None = None) -> None:
        self.code: str = code
        self.message: str | None = message
        # Forward the most useful single-line representation to
        # ``Exception.__init__`` so ``logging`` / ``traceback`` modules
        # show the same string ``str(self)`` produces below.
        if message is None:
            super().__init__(code)
        else:
            super().__init__(f"{code}: {message}")

    def __str__(self) -> str:
        if self.message is None:
            return self.code
        return f"{self.code}: {self.message}"


# ---------------------------------------------------------------------------
# BedrockSamplingBackend — routes the prompt through bedrock-runtime:Converse
# ---------------------------------------------------------------------------


class BedrockSamplingBackend:
    """Sampling backend that calls ``bedrock-runtime:Converse``.

    The backend resolves its model id and region at construction time
    from (in order of precedence) the explicit constructor argument,
    the matching environment variable
    (:data:`ENV_BEDROCK_MODEL_ID` / :data:`ENV_BEDROCK_REGION`), and
    finally the ``cdk.json`` Mission default
    (:func:`gco.bedrock.get_default_mission_model_id` /
    :data:`DEFAULT_BEDROCK_REGION`). The ``boto3`` client itself is
    constructed lazily on the first :meth:`sample` call so that
    ``import mission.sampling`` does not pull ``boto3`` into the
    import graph and so that test code can swap the import in via
    ``unittest.mock.patch`` without paying for a real session at
    construction time.

    Failure modes:

    * Missing or partial AWS credentials at client-construction time
      surface as :class:`SamplingTransportError` with code
      ``"bedrock_no_credentials"``; the original exception is chained
      via ``__cause__``.
    * A ``botocore.exceptions.ClientError`` from the ``Converse`` call
      surfaces as :class:`SamplingTransportError` with code
      ``"bedrock_<ErrorCode>"`` where ``<ErrorCode>`` is read from the
      error envelope (defaulting to ``"Unknown"`` when the envelope is
      malformed). The one exception is the Anthropic first-time-use
      gate, which raises
      :class:`gco.bedrock.BedrockFTUFormNotAcceptedError` instead of a
      transport error so it is never absorbed by a deterministic
      fallback. See ``docs/CUSTOMIZATION.md`` (Bedrock Model Selection).
    * A response without a non-empty ``text`` block under
      ``output.message.content`` — including reasoning-only and empty
      ``content`` lists — surfaces as :class:`SamplingTransportError`
      with code ``"bedrock_malformed_response"``.
    * A response cut off by an output-token limit
      (``stopReason == "max_tokens"``) surfaces as
      :class:`SamplingTransportError` with code
      ``"bedrock_truncated_response"``.
    """

    backend_name: Literal["bedrock"] = "bedrock"

    def __init__(
        self,
        model_id: str | None = None,
        region: str | None = None,
    ) -> None:
        """Resolve the model id and region; defer client construction.

        Args:
            model_id: Optional explicit model id. When ``None``, falls
                back to the :data:`ENV_BEDROCK_MODEL_ID` environment
                variable, then to the ``cdk.json`` Mission default from
                :func:`gco.bedrock.get_default_mission_model_id`.
            region: Optional explicit region. When ``None``, falls back
                to :data:`ENV_BEDROCK_REGION`, then to
                :data:`DEFAULT_BEDROCK_REGION`.
        """
        if model_id is not None:
            self.model_id = model_id
            self._uses_default_model = False
        elif ENV_BEDROCK_MODEL_ID in os.environ:
            # Preserve the existing explicit-environment semantics, including
            # an intentionally empty value, without evaluating the fallback.
            self.model_id = os.environ[ENV_BEDROCK_MODEL_ID]
            self._uses_default_model = False
        else:
            self.model_id = get_default_mission_model_id()
            self._uses_default_model = True
        self._region: str = (
            region
            if region is not None
            else os.environ.get(ENV_BEDROCK_REGION, DEFAULT_BEDROCK_REGION)
        )
        # The boto3 client is built on first ``sample`` call. ``None``
        # here is the sentinel for "not yet constructed".
        self._client: Any = None

    @classmethod
    def from_canonical_default(
        cls,
        region: str | None = None,
    ) -> BedrockSamplingBackend:
        """Build a backend that deliberately applies canonical reasoning.

        Unlike ``cls(model_id=None)``, this bypasses the model environment
        override. Fixture capture uses it to reproduce the checked-in default
        exactly, while ordinary explicit model IDs retain override semantics.
        """
        backend = cls(model_id=get_default_mission_model_id(), region=region)
        backend._uses_default_model = True
        return backend

    def _get_client(self) -> Any:
        """Return the cached ``bedrock-runtime`` client, building it on first use.

        ``boto3`` and ``botocore.exceptions`` are imported here rather
        than at module top-level so that pure-Python consumers of this
        module (the prompt builder, the protocol, the error type) do
        not pay for the ``boto3`` import. This also lets tests patch
        ``mission.sampling.boto3`` after import.
        """
        if self._client is not None:
            return self._client
        # Local import — keeps the module's import surface boto3-free.
        import boto3
        from botocore.config import Config
        from botocore.exceptions import (
            NoCredentialsError,
            PartialCredentialsError,
        )

        try:
            self._client = boto3.Session().client(
                "bedrock-runtime",
                region_name=self._region,
                config=Config(read_timeout=BEDROCK_READ_TIMEOUT_SECONDS),
            )
        except (NoCredentialsError, PartialCredentialsError) as err:
            raise SamplingTransportError("bedrock_no_credentials") from err
        return self._client

    async def sample(self, prompt: SamplingPrompt) -> str:
        """Render ``prompt`` through ``Converse`` and return the response text.

        Raises:
            SamplingTransportError: On any transport-level failure.
                * ``bedrock_no_credentials`` — credentials could not be
                  resolved by ``boto3`` at client-construction time.
                * ``bedrock_<ErrorCode>`` — the ``Converse`` call raised
                  a ``ClientError``; ``<ErrorCode>`` is the AWS error
                  code from the envelope.
                * ``bedrock_malformed_response`` — the response did not
                  contain a non-empty final text content block.
                * ``bedrock_truncated_response`` — the response was cut
                  off by an output-token limit and cannot be trusted to
                  be complete.
            gco.bedrock.BedrockFTUFormNotAcceptedError: The account has
                not submitted Anthropic's one-time first-time-use case
                form. Raised instead of a transport error so callers
                cannot silently fall back past a permanent, one-line-fix
                misconfiguration.
        """
        # Local import — see ``_get_client`` for the rationale.
        from botocore.exceptions import ClientError

        client = self._get_client()

        text = prompt.assemble()
        converse_options = build_bedrock_converse_options(
            self.model_id,
            # Deliberately no maxTokens: the Converse default is the model's
            # own maximum output length, so a rationale can never be cut off
            # by a GCO-imposed cap. A cap is opt-in — pass maxTokens here to
            # restore one.
            inference_config={"temperature": BEDROCK_TEMPERATURE},
            apply_default_reasoning=self._uses_default_model,
        )
        try:
            response = await asyncio.to_thread(
                client.converse,
                modelId=self.model_id,
                messages=[{"role": "user", "content": [{"text": text}]}],
                **converse_options,
            )
        except ClientError as err:
            # A missing Anthropic FTU form is a permanent account-scoped
            # misconfiguration, not a transport fault: escalate it instead of
            # letting the deterministic-fallback path absorb it silently.
            raise_if_bedrock_ftu_form_error(err)
            # ``e.response`` is documented to be present on ClientError
            # but the envelope shape can vary; defend against missing
            # keys so the audit pipeline always sees a tagged code.
            envelope = getattr(err, "response", None) or {}
            error_block = envelope.get("Error", {}) if isinstance(envelope, dict) else {}
            code = (
                error_block.get("Code", "Unknown") if isinstance(error_block, dict) else "Unknown"
            )
            raise SamplingTransportError(f"bedrock_{code}") from err

        # Capture token usage from the Converse response for the audit
        # trail. The ``usage`` block is present on every successful
        # Converse response and carries ``inputTokens`` and
        # ``outputTokens``. Store on the instance so callers can read
        # it after each sample() call without changing the protocol.
        usage = response.get("usage") or {}
        self.last_input_tokens: int | None = usage.get("inputTokens")
        self.last_output_tokens: int | None = usage.get("outputTokens")

        try:
            return extract_bedrock_converse_text(response)
        except BedrockResponseTruncatedError as err:
            # A cut-off rationale is unusable; let the deterministic-fallback
            # path absorb it like any other transport-shaped fault.
            raise SamplingTransportError("bedrock_truncated_response") from err
        except (KeyError, IndexError, TypeError) as err:
            raise SamplingTransportError("bedrock_malformed_response") from err


# ---------------------------------------------------------------------------
# Backend resolver
# ---------------------------------------------------------------------------


def select_sampling_backend(model_id: str | None) -> SamplingBackend:
    """Construct the sampling backend for a session that opted into sampling.

    Bedrock is the only sampling transport. MCP client sampling
    (``ctx.sample``) left the protocol with FastMCP 4's sessionless era —
    per the v4 migration guidance, generation belongs server-side — so
    missions sample through ``bedrock-runtime:Converse`` with the server's
    own credentials regardless of whether they were started from the CLI
    or over MCP. Credential resolution is deferred to the first ``sample``
    call; a missing-credentials failure surfaces as
    :class:`SamplingTransportError` and the engine's deterministic
    fallback absorbs it.

    Args:
        model_id: Optional concrete model identifier. Forwarded to the
            backend constructor verbatim; ``None`` resolves through the
            environment and ``cdk.json`` Mission default.

    Returns:
        A :class:`BedrockSamplingBackend` bound to ``model_id``.
    """
    return BedrockSamplingBackend(model_id)


# ---------------------------------------------------------------------------
# Strategy-against-catalog validator
# ---------------------------------------------------------------------------


def _resolve_input_schema(tool: Any) -> Any:
    """Return the registered Pydantic input model for a Tool, or None.

    FastMCP exposes the model under ``input_schema`` in newer releases
    and ``inputSchema`` in older ones. Tolerate both. Tools that genuinely
    take no args (or test catalog mocks that omit the attribute) yield
    ``None``, in which case the caller skips per-call args validation.
    """
    schema = getattr(tool, "input_schema", None)
    if schema is None:
        schema = getattr(tool, "inputSchema", None)
    return schema


def _extract_tool_json_schemas(
    allowlist: Sequence[str],
    registered_tools: Mapping[str, Any],
) -> dict[str, Any]:
    """Extract JSON Schema dicts for each allowlisted tool's input parameters.

    Calls ``.model_json_schema()`` on the Pydantic model exposed by
    ``_resolve_input_schema``. Falls back gracefully: tools without a
    schema, tools whose schema isn't a Pydantic model, and any
    exception during schema extraction all yield ``None`` for that
    tool (omitted from the output dict). The caller renders the
    result into the Strategy_Revision prompt so the model can propose
    valid ``args`` dicts.
    """
    schemas: dict[str, Any] = {}
    for name in allowlist:
        tool = registered_tools.get(name)
        if tool is None:
            continue
        model = _resolve_input_schema(tool)
        if model is None:
            continue
        try:
            # Pydantic v2 models expose model_json_schema() as a classmethod.
            json_schema = model.model_json_schema()
            schemas[name] = json_schema
        except Exception:
            # Non-Pydantic schema, or a mock that doesn't support it.
            continue
    return schemas


def validate_strategy_against_catalog(
    strategy: Strategy,
    allowlist: list[str],
    registered_tools: dict[str, Any],
    allow_scripts: bool,
) -> None:
    """Validate a Strategy against the live tool catalog.

    Returns ``None`` on accept; raises :class:`MissionValidationError`
    with a structured ``details.reason`` enum on reject. The function
    layers catalog-aware checks on top of the structural validation in
    :func:`mission.validation.validate_strategy`:

    1. Mutual-exclusivity (exactly one of ``tool_calls`` / ``script``).
    2. Per-call ``tool_name`` is in ``allowlist``.
    3. Per-call ``args`` validates against the registered Pydantic model
       exposed under ``Tool.input_schema`` (or the older
       ``Tool.inputSchema``); calls whose tool has neither attribute or
       a ``None`` schema skip args validation.
    4. For scripted strategies, ``allow_scripts`` is True and the
       script's AST passes :func:`mission.sandbox.validate_script_ast`.

    Args:
        strategy: The Strategy dict to validate.
        allowlist: The session's resolved Tool_Allowlist.
        registered_tools: Mapping from tool name to a registered tool
            object (typed ``Any`` so the module imports without
            FastMCP). Read-only — only ``input_schema`` /
            ``inputSchema`` is consulted.
        allow_scripts: Session-level flag gating scripted strategies.
    """
    # 1. Structural validation: mutual exclusivity, script-allow gating,
    #    and AST validation for scripts. Reuses the existing validator
    #    so error shapes for those rejection classes stay aligned with
    #    the rest of the input pipeline.
    _validation.validate_strategy(cast("dict[str, Any]", strategy), allowlist, allow_scripts)

    # The structural validator has already accepted exactly one of the
    # two shapes. Branch on which one is present.
    if "tool_calls" in strategy:
        tool_calls = strategy["tool_calls"]
        # Empty list is rejected by validate_strategy; this is a defence
        # in depth for callers that might bypass that path.
        if not tool_calls:
            raise MissionValidationError(
                "validation_error",
                details={
                    "field": "strategy",
                    "subfield": "tool_calls",
                    "reason": "tool_calls_empty",
                },
            )

        # 2. Per-call name-in-allowlist check.
        for call in tool_calls:
            name = call.get("tool_name")
            if name not in allowlist:
                raise MissionValidationError(
                    "validation_error",
                    details={
                        "field": "strategy",
                        "subfield": "tool_calls",
                        "tool_name": name,
                        "reason": "tool_not_allowlisted",
                        "allowlist": list(allowlist),
                    },
                )

        # 3. Per-call args validation against the tool's Pydantic model.
        for call in tool_calls:
            name = call["tool_name"]
            tool = registered_tools.get(name)
            if tool is None:
                # Catalog could have a name in the allowlist that is not
                # currently registered (gating, dynamic load). Mirror the
                # unknown-tool shape used elsewhere.
                raise MissionValidationError(
                    "validation_error",
                    details={
                        "field": "strategy",
                        "subfield": "tool_calls",
                        "tool_name": name,
                        "reason": "tool_not_registered",
                    },
                )
            schema = _resolve_input_schema(tool)
            if schema is None:
                # Either the tool genuinely takes no args, or the test
                # catalog omitted a model. Skip args validation rather
                # than reject — the design treats missing schema as
                # "trust the dispatcher".
                continue
            args = call.get("args", {})
            if not isinstance(args, dict):
                raise MissionValidationError(
                    "validation_error",
                    details={
                        "field": "strategy",
                        "subfield": "tool_calls",
                        "tool_name": name,
                        "reason": "tool_args_invalid",
                        "errors": [
                            {
                                "type": "args_not_a_dict",
                                "actual_type": type(args).__name__,
                            }
                        ],
                    },
                )
            try:
                schema.model_validate(args)
            except Exception as exc:  # noqa: BLE001 - pydantic ValidationError + similar
                # Pydantic v2 ValidationError exposes ``.errors()`` as a
                # list of structured dicts. Tolerate any other exception
                # type (e.g. older Pydantic, custom validators) by
                # falling back to ``str(exc)``.
                errors_method = getattr(exc, "errors", None)
                if callable(errors_method):
                    try:
                        errors_payload: Any = errors_method()
                    except Exception:  # noqa: BLE001 - defensive
                        errors_payload = [{"type": "unknown", "msg": str(exc)}]
                else:
                    errors_payload = [{"type": "unknown", "msg": str(exc)}]
                raise MissionValidationError(
                    "validation_error",
                    details={
                        "field": "strategy",
                        "subfield": "tool_calls",
                        "tool_name": name,
                        "reason": "tool_args_invalid",
                        "errors": errors_payload,
                    },
                ) from exc

        # 4. Cost estimation against remaining budget. Removed —
        #    cost guardrails live out-of-band via AWS Budgets / Cost
        #    Anomaly Detection rather than in the Mission cascade.
    # Scripted strategies: validate_strategy already ran allow_scripts
    # gating and the AST validator. No catalog-aware checks are layered
    # on top here — the script-side enforcement happens at execute time
    # via the in-script tool callable wrappers.


# ---------------------------------------------------------------------------
# Orchestration helpers — bind a backend to a SessionState and return either
# a used result or a deterministic fallback.
# ---------------------------------------------------------------------------

# Local imports kept inside this section so the prompt-builder /
# backend half above stays free of audit / decide dependencies.
from . import audit as _mission_audit  # noqa: E402
from . import decide as _decide  # noqa: E402

# Type alias used by the helpers below. ``SessionState`` is a TypedDict
# whose runtime value is just ``dict``; the alias keeps the signatures
# expressive without forcing the import to leak through ``__all__``.
from .types import SessionState as _SessionState  # noqa: E402


@dataclass(frozen=True)
class SamplingUsed:
    """A successful sampling call's accepted output."""

    output_text: str
    """Raw model output (the text returned by the bound backend)."""

    parsed: dict[str, Any]
    """Parsed JSON payload that has cleared the schema and catalog checks."""

    backend_name: Literal["bedrock"]
    """Stable backend identifier — echoes the bound backend's tag."""

    model_id: str
    """The concrete model id the backend routed the prompt to."""


@dataclass(frozen=True)
class SamplingFallback:
    """A rejected or unavailable sampling call's deterministic substitute.

    Returned when the bound backend was ``None``, the transport raised,
    the model output failed to parse / validate, or any catalog or
    budget check rejected the proposed strategy. The ``rationale`` is
    a pure function of the bound :class:`SessionState` and the most
    recent :class:`IterationRecord`, so the engine can replay or
    reproduce a fallback exactly from persisted state.
    """

    rationale: str
    """Deterministic fallback text. Empty string for ``final_lessons``
    — the final-report writer fills in its own deterministic text in
    that case."""

    reason: str
    """Stable token tagging *why* the fallback fired. Examples:
    ``"transport_error"``, ``"json_parse"``, ``"schema_mismatch"``,
    ``"tool_not_allowlisted"``, ``"tool_args_invalid"``,
    ``"over_budget"``, ``"script_rejected"``,
    ``"no_backend_resolved"``, ``"disabled"``."""

    backend_name: Literal["bedrock", "none"]
    """The bound backend's tag, or ``"none"`` when no backend was
    resolved at the call site."""

    model_id: str | None
    """The bound backend's model id, or ``None`` when no backend was
    resolved."""


# ---------------------------------------------------------------------------
# JSON / schema helpers (private to the orchestration layer)
# ---------------------------------------------------------------------------


def _extract_json_object(text: str) -> dict[str, Any]:
    """Parse the first JSON object embedded in ``text``.

    Models routinely wrap JSON in prose. The implementation slices from
    the first ``{`` to the last ``}`` and feeds the result to
    :func:`json.loads`. When no balanced braces are present, or the
    sliced substring is not valid JSON, the function raises
    :class:`json.JSONDecodeError` so the caller can branch on a single
    well-typed exception.
    """
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end < start:
        # No braces at all → treat as a parse error so the calling
        # branch surfaces ``reason="json_parse"``.
        raise json.JSONDecodeError("no JSON object found", text, 0)
    candidate = text[start : end + 1]
    parsed = json.loads(candidate)
    if not isinstance(parsed, dict):
        # The sliced substring parsed but is not an object — surface as
        # a parse error too, since downstream code requires a dict.
        raise json.JSONDecodeError("top-level JSON value is not an object", candidate, 0)
    return parsed


def _validate_revision_schema(parsed: dict[str, Any]) -> None:
    """Reject a parsed payload that is not a valid Strategy_Revision.

    Required keys: ``revision_rationale`` (non-empty str),
    ``next_strategy`` (dict), ``confidence`` (number in [0, 1]).
    """
    rationale = parsed.get("revision_rationale")
    if not isinstance(rationale, str) or not rationale:
        raise ValueError("schema_mismatch: revision_rationale must be non-empty str")
    next_strategy = parsed.get("next_strategy")
    if not isinstance(next_strategy, dict):
        raise ValueError("schema_mismatch: next_strategy must be a dict")
    confidence = parsed.get("confidence")
    # ``bool`` is excluded explicitly — it is a subclass of ``int`` in
    # Python and would otherwise sneak past the numeric check.
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
        raise ValueError("schema_mismatch: confidence must be a number")
    if not (0.0 <= float(confidence) <= 1.0):
        raise ValueError("schema_mismatch: confidence must be in [0, 1]")


def _validate_lessons_schema(parsed: dict[str, Any]) -> None:
    """Reject a parsed payload that is not a valid final-lessons dict.

    Required keys: ``lessons`` (non-empty list of non-empty str),
    ``recommended_followups`` (list of str — may be empty).
    """
    lessons = parsed.get("lessons")
    if not isinstance(lessons, list) or not lessons:
        raise ValueError("schema_mismatch: lessons must be a non-empty list")
    for item in lessons:
        if not isinstance(item, str) or not item:
            raise ValueError("schema_mismatch: each lesson must be a non-empty str")
    followups = parsed.get("recommended_followups")
    if not isinstance(followups, list):
        raise ValueError("schema_mismatch: recommended_followups must be a list")
    for item in followups:
        if not isinstance(item, str):
            raise ValueError("schema_mismatch: each follow-up must be a str")


# ---------------------------------------------------------------------------
# maybe_sample_strategy_revision
# ---------------------------------------------------------------------------


async def maybe_sample_strategy_revision(
    *,
    backend: SamplingBackend | None,
    session: _SessionState,
    iteration: IterationRecord,
    allowlist: list[str],
    registered_tools: dict[str, Any],
    tool_docstrings: dict[str, str],
    remaining_iterations: int,
    remaining_wall_clock_secs: float | None,
    allow_scripts: bool,
    environment_context: Mapping[str, Any] | None = None,
    prior_missions: Sequence[Mapping[str, Any]] | None = None,
) -> SamplingUsed | SamplingFallback:
    """Consult the advisory LLM for a Strategy_Revision, or fall back.

    Returns a :class:`SamplingUsed` when the bound backend produces a
    JSON object that clears schema validation and the catalog checks.
    Returns a :class:`SamplingFallback` carrying the deterministic
    rationale from
    :func:`mission.decide.build_revision_rationale_template` on every
    rejection class. Emits exactly one
    :func:`mission.audit.emit_sampling_event` per call.
    """
    session_id = session["session_id"]
    iteration_index = iteration["iteration_index"]
    template = _decide.build_revision_rationale_template(session, iteration)

    # ---- No backend resolved: short-circuit. ------------------------------
    if backend is None:
        _mission_audit.emit_sampling_event(
            session_id,
            iteration_index,
            sampling_purpose="strategy_revision",
            sampling_status="disabled",
            sampling_backend="none",
        )
        return SamplingFallback(
            rationale=template,
            reason="no_backend_resolved",
            backend_name="none",
            model_id=None,
        )

    backend_name = backend.backend_name
    model_id = backend.model_id

    # ---- Build the prompt. ------------------------------------------------
    # The in-progress iteration that triggered ``adjust`` is already in
    # ``session["iterations"][-1]``, so the most-recent-five window is a
    # plain slice; ``RECENT_ITERATIONS_LIMIT`` is enforced inside the
    # prompt builder as a defensive cap.
    recent_iterations = list(session["iterations"][-RECENT_ITERATIONS_LIMIT:])
    tool_schemas = _extract_tool_json_schemas(allowlist, registered_tools)
    prompt = SamplingPrompt(
        directive=session["directive_text"],
        success_criteria=session["criteria"],
        criteria_status=iteration["criteria_evaluation"],
        recent_iterations=recent_iterations,
        tool_allowlist=allowlist,
        tool_docstrings=tool_docstrings,
        remaining_iterations=remaining_iterations,
        remaining_wall_clock_secs=remaining_wall_clock_secs,
        allow_scripts=allow_scripts,
        tool_schemas=tool_schemas,
        environment_context=environment_context,
        prior_missions=prior_missions,
    )

    # ---- Transport: backend.sample. --------------------------------------
    try:
        output_text = await backend.sample(prompt)
    except SamplingTransportError as err:
        _mission_audit.emit_sampling_event(
            session_id,
            iteration_index,
            sampling_purpose="strategy_revision",
            sampling_status="rejected",
            sampling_backend=backend_name,
            sampling_model_id=model_id or None,
            validation_error=err.code,
        )
        return SamplingFallback(
            rationale=template,
            reason="transport_error",
            backend_name=backend_name,
            model_id=model_id,
        )

    # ---- Parse the output as JSON. ---------------------------------------
    try:
        parsed = _extract_json_object(output_text)
    except json.JSONDecodeError:
        _mission_audit.emit_sampling_event(
            session_id,
            iteration_index,
            sampling_purpose="strategy_revision",
            sampling_status="rejected",
            sampling_backend=backend_name,
            sampling_model_id=model_id or None,
            validation_error="json_parse",
        )
        return SamplingFallback(
            rationale=template,
            reason="json_parse",
            backend_name=backend_name,
            model_id=model_id,
        )

    # ---- Schema validation. ----------------------------------------------
    try:
        _validate_revision_schema(parsed)
    except ValueError:
        _mission_audit.emit_sampling_event(
            session_id,
            iteration_index,
            sampling_purpose="strategy_revision",
            sampling_status="rejected",
            sampling_backend=backend_name,
            sampling_model_id=model_id or None,
            validation_error="schema_mismatch",
        )
        return SamplingFallback(
            rationale=template,
            reason="schema_mismatch",
            backend_name=backend_name,
            model_id=model_id,
        )

    # ---- Catalog validation on the proposed next_strategy. ---------------
    try:
        validate_strategy_against_catalog(
            parsed["next_strategy"],
            allowlist,
            registered_tools,
            allow_scripts,
        )
    except MissionValidationError as err:
        # ``err.details["reason"]`` carries the structured rejection
        # token (e.g. ``"tool_not_allowlisted"``,
        # ``"tool_args_invalid"``). Fall back to a generic label when
        # the validator emits a rejection without a ``reason`` key.
        details = err.details or {}
        reason = details.get("reason", "validation_error")
        _mission_audit.emit_sampling_event(
            session_id,
            iteration_index,
            sampling_purpose="strategy_revision",
            sampling_status="rejected",
            sampling_backend=backend_name,
            sampling_model_id=model_id or None,
            validation_error=str(reason),
        )
        return SamplingFallback(
            rationale=template,
            reason=str(reason),
            backend_name=backend_name,
            model_id=model_id,
        )

    # ---- Success path. ---------------------------------------------------
    # Extract token usage from the backend if available (Bedrock backend
    # stores it as a side-channel after each sample() call).
    _input_tokens = getattr(backend, "last_input_tokens", None)
    _output_tokens = getattr(backend, "last_output_tokens", None)
    _mission_audit.emit_sampling_event(
        session_id,
        iteration_index,
        sampling_purpose="strategy_revision",
        sampling_status="used",
        sampling_backend=backend_name,
        sampling_model_id=model_id or None,
        model_output_bytes=len(output_text.encode("utf-8")),
        input_tokens=_input_tokens,
        output_tokens=_output_tokens,
    )
    return SamplingUsed(
        output_text=output_text,
        parsed=parsed,
        backend_name=backend_name,
        model_id=model_id,
    )


# ---------------------------------------------------------------------------
# maybe_sample_final_lessons
# ---------------------------------------------------------------------------


async def maybe_sample_final_lessons(
    *,
    backend: SamplingBackend | None,
    session: _SessionState,
    remaining_iterations: int = 0,
    remaining_wall_clock_secs: float | None = None,
    allow_scripts: bool = False,
    tool_docstrings: dict[str, str] | None = None,
    environment_context: Mapping[str, Any] | None = None,
) -> SamplingUsed | SamplingFallback:
    """Consult the advisory LLM for final lessons, or fall back.

    Returns a :class:`SamplingUsed` when the bound backend produces a
    JSON object that clears the lessons schema. Returns a
    :class:`SamplingFallback` with an *empty* rationale on every
    rejection class — the final-report writer is responsible for the
    deterministic-text path when sampling does not produce usable
    output. Emits exactly one
    :func:`mission.audit.emit_sampling_event` per call, with
    ``iteration_index_or_purpose=None`` since the call is out-of-loop.
    """
    session_id = session["session_id"]

    # ---- No backend resolved: short-circuit. ------------------------------
    if backend is None:
        _mission_audit.emit_sampling_event(
            session_id,
            None,
            sampling_purpose="final_lessons",
            sampling_status="disabled",
            sampling_backend="none",
        )
        return SamplingFallback(
            rationale="",
            reason="no_backend_resolved",
            backend_name="none",
            model_id=None,
        )

    backend_name = backend.backend_name
    model_id = backend.model_id

    # ---- Build the prompt. ------------------------------------------------
    # Pass *all* iterations; the prompt builder trims / drops as needed
    # to fit the byte budget.
    prompt = SamplingPrompt(
        directive=session["directive_text"],
        success_criteria=session["criteria"],
        # The lessons prompt has no per-iteration criteria status; the
        # builder still expects the field, so reuse the most recent
        # iteration's evaluation when available, else an empty list.
        criteria_status=(
            list(session["iterations"][-1]["criteria_evaluation"]) if session["iterations"] else []
        ),
        recent_iterations=list(session["iterations"]),
        tool_allowlist=session.get("tool_allowlist", []),
        tool_docstrings=tool_docstrings or {},
        remaining_iterations=remaining_iterations,
        remaining_wall_clock_secs=remaining_wall_clock_secs,
        allow_scripts=allow_scripts,
        environment_context=environment_context,
    )

    # ---- Transport: backend.sample (uses lessons assembler). -------------
    try:
        # We render the lessons-specific prompt here so the byte-cap
        # bookkeeping uses the right schema header. The backend's own
        # ``sample`` calls ``prompt.assemble()`` under the hood for the
        # Strategy_Revision flow, but for lessons we assemble here and
        # invoke a thin shim through the backend.
        rendered = prompt.assemble_final_lessons()
        output_text = await _sample_with_assembled_text(backend, rendered)
    except SamplingTransportError as err:
        _mission_audit.emit_sampling_event(
            session_id,
            None,
            sampling_purpose="final_lessons",
            sampling_status="rejected",
            sampling_backend=backend_name,
            sampling_model_id=model_id or None,
            validation_error=err.code,
        )
        return SamplingFallback(
            rationale="",
            reason="transport_error",
            backend_name=backend_name,
            model_id=model_id,
        )

    # ---- Parse the output as JSON. ---------------------------------------
    try:
        parsed = _extract_json_object(output_text)
    except json.JSONDecodeError:
        _mission_audit.emit_sampling_event(
            session_id,
            None,
            sampling_purpose="final_lessons",
            sampling_status="rejected",
            sampling_backend=backend_name,
            sampling_model_id=model_id or None,
            validation_error="json_parse",
        )
        return SamplingFallback(
            rationale="",
            reason="json_parse",
            backend_name=backend_name,
            model_id=model_id,
        )

    # ---- Schema validation. ----------------------------------------------
    try:
        _validate_lessons_schema(parsed)
    except ValueError:
        _mission_audit.emit_sampling_event(
            session_id,
            None,
            sampling_purpose="final_lessons",
            sampling_status="rejected",
            sampling_backend=backend_name,
            sampling_model_id=model_id or None,
            validation_error="schema_mismatch",
        )
        return SamplingFallback(
            rationale="",
            reason="schema_mismatch",
            backend_name=backend_name,
            model_id=model_id,
        )

    # ---- Success path. ---------------------------------------------------
    _input_tokens = getattr(backend, "last_input_tokens", None)
    _output_tokens = getattr(backend, "last_output_tokens", None)
    _mission_audit.emit_sampling_event(
        session_id,
        None,
        sampling_purpose="final_lessons",
        sampling_status="used",
        sampling_backend=backend_name,
        sampling_model_id=model_id or None,
        model_output_bytes=len(output_text.encode("utf-8")),
        input_tokens=_input_tokens,
        output_tokens=_output_tokens,
    )
    return SamplingUsed(
        output_text=output_text,
        parsed=parsed,
        backend_name=backend_name,
        model_id=model_id,
    )


async def _sample_with_assembled_text(backend: SamplingBackend, rendered: str) -> str:
    """Route a pre-assembled prompt string through a backend.

    Both shipped backends accept a :class:`SamplingPrompt` and call
    ``assemble`` themselves to render the strategy-revision shape. For
    the final-lessons path we render the lessons-shaped prompt here
    and need to deliver that exact text to the transport. The shim
    builds a tiny prompt-shaped wrapper whose :meth:`assemble` returns
    the pre-rendered text and forwards it to the backend.
    """
    pre_rendered = rendered

    class _PreRendered:
        """Thin :class:`SamplingPrompt` look-alike with a fixed assemble()."""

        def assemble(self) -> str:
            return pre_rendered

    # The two shipped backends only call ``prompt.assemble()`` so the
    # duck-typed wrapper above is enough to drive either of them.
    return await backend.sample(_PreRendered())  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Session-start sampling-state resolver
# ---------------------------------------------------------------------------


def _bedrock_credentials_available() -> bool:
    """Lightweight probe: do local AWS credentials resolve?

    Instantiates a ``boto3.Session()`` and asks for ``get_credentials()``
    without making any network call. ``boto3`` is imported inside the
    function so the module's top-level import surface stays free of
    SDK dependencies — and so a host that has no ``boto3`` installed
    (or any other unexpected import-time failure) cleanly degrades to
    "no credentials available" rather than crashing the helper.
    """
    try:
        import boto3

        session = boto3.Session()
        creds = session.get_credentials()
        return creds is not None
    except Exception:
        return False


def resolve_sampling_state(
    use_sampling_param: bool | None,
) -> tuple[bool, Literal["bedrock", "none"]]:
    """Decide whether sampling is enabled for a session and which backend resolves.

    Bedrock is the only sampling transport (MCP client sampling left the
    protocol with FastMCP 4), so resolution no longer depends on how the
    session was started — CLI and MCP callers probe the same server-side
    credentials.

    Resolution precedence (first match wins):

    1. ``use_sampling_param is False`` — caller explicitly disabled
       sampling, so the result is ``(False, "none")`` regardless of
       any capability the environment advertises.
    2. Local AWS credentials resolve — ``(True, "bedrock")``.
    3. No credentials — ``(True, "none")`` if the caller opted in
       explicitly with ``use_sampling_param is True`` (so the caller can
       decide whether to error or proceed deterministic-only), and
       ``(False, "none")`` otherwise.

    Args:
        use_sampling_param: Three-state opt-in flag. ``None`` means the
            caller did not specify and the helper should auto-detect.
            ``False`` short-circuits to a disabled state. ``True`` means
            the caller explicitly opted in; the backend is auto-detected
            and ``"none"`` is allowed when no concrete backend resolves.

    Returns:
        A ``(use_sampling, backend)`` tuple. The caller persists both
        values on its ``SessionState`` so the audit pipeline can stamp
        every later sampling event with the resolved backend.
    """
    # 1. Explicit opt-out wins outright.
    if use_sampling_param is False:
        return (False, "none")

    # 2. Probe server-side AWS credentials.
    if _bedrock_credentials_available():
        return (True, "bedrock")

    # 3. No credentials — only honour an explicit True.
    if use_sampling_param is True:
        return (True, "none")
    return (False, "none")
