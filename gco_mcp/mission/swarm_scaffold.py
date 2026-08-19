"""Swarm_Plan generation: sampled decomposition, deterministic fallback.

A Swarm_Plan is a list of **spawn requests** — plain JSON-safe dicts in
exactly the shape :func:`mission.swarm.validate_spawn` accepts — so a
plan can be printed for review, saved to disk, and fed straight to the
runner's spawn seam. Every plan this module returns has already been
admission-validated end to end (fleet cap, iteration pool, finite child
budgets, allowlist exclusions, and mutating-tool overlap are enforced
*across* the plan by simulating the registry the spawns would build), so
a returned plan cannot be rejected at spawn time against the same config
and registered-tool set.

Two producers, mirroring the criteria scaffolder one level up:

* :func:`generate_sampled_plan` — asks a sampling backend for a JSON
  array of child specs, validates every entry, and feeds rejection
  reasons back into a bounded retry loop
  (:func:`mission.criteria_scaffold.generate_sampled_criteria` is the
  precedent). Exhaustion raises :class:`SwarmScaffoldError`; callers
  fall back to the deterministic path, warning once, exactly like
  ``gco mission run`` does.
* :func:`generate_deterministic_plan` — always available, no sampling,
  no AWS: a single child mirroring the swarm directive with criteria
  from the deterministic criteria scaffold. Degenerate on purpose — a
  swarm of one is safe and correct when nothing smarter is available,
  and it is the CI path.

The advisory-only boundary holds: sampling proposes plans, the pure
validators admit them, and nothing in this module touches a verdict.
:func:`sample_revised_directive` supplies the optional
``on_failure_with_revision`` directive text for respawns — again advisory
text only; the respawn decision lives in the deterministic restart table.
"""

from __future__ import annotations

import json
from collections.abc import Collection, Mapping
from typing import Any, Final

from gco.bedrock import BedrockFTUFormNotAcceptedError

from . import criteria_scaffold
from . import swarm as swarm_rules
from .types import SessionState, SwarmConfig
from .validation import MissionValidationError, validate_directive

__all__ = [
    "DEFAULT_CHILD_MAX_ITERATIONS",
    "DEFAULT_CHILD_MAX_WALL_CLOCK_SECONDS",
    "DEFAULT_PLAN_RETRIES",
    "SwarmScaffoldError",
    "build_plan_prompt",
    "generate_deterministic_plan",
    "generate_sampled_plan",
    "sample_revised_directive",
    "validate_plan",
]

DEFAULT_CHILD_MAX_ITERATIONS: Final[int] = 5
"""Default per-child iteration budget when the caller supplies none."""

DEFAULT_CHILD_MAX_WALL_CLOCK_SECONDS: Final[int] = 300
"""Default per-child wall-clock budget when the caller supplies none."""

DEFAULT_PLAN_RETRIES: Final[int] = 3
"""Sampled-path retry budget before deterministic fallback."""

_PROMPT_TOOL_LIMIT: Final[int] = 40
"""Cap on catalog entries rendered into the plan prompt."""

_PROMPT_DOCSTRING_CHARS: Final[int] = 200
"""Per-tool docstring budget in the plan prompt."""


class SwarmScaffoldError(Exception):
    """Every sampled plan attempt was rejected.

    ``last_reason`` carries the final rejection token (a validator
    ``details.reason``, ``json_parse``, or ``transport_error``) so the
    caller's fallback warning names the cause.
    """

    def __init__(self, last_reason: str, message: str | None = None) -> None:
        self.last_reason = last_reason
        super().__init__(message if message is not None else last_reason)


def _spec_to_request(spec: swarm_rules.SpawnSpec) -> dict[str, Any]:
    """Serialize a validated SpawnSpec back into a JSON-safe spawn request.

    Criteria drop the validator's cached ``_parsed_ast`` so the request
    round-trips through ``json.dumps``; re-validation re-parses on
    demand. The request re-admits by construction against the same
    config and registered-tool inputs it was validated with.
    """
    criteria = [
        {k: v for k, v in criterion.items() if not str(k).startswith("_")}
        for criterion in spec["criteria"]
    ]
    return {
        "slot": spec["slot"],
        "directive": spec["directive"],
        "criteria": criteria,
        "budget": dict(spec["budget"]),
        "tool_allowlist": list(spec["tool_allowlist"]),
        "cadence": dict(spec["checkpoint_cadence"]),
        "restart_policy": spec["restart_policy"],
        "max_respawns": spec["max_respawns"],
        "use_sampling": spec["use_sampling"],
    }


def validate_plan(
    entries: list[dict[str, Any]],
    *,
    config: SwarmConfig,
    registered_tools: dict[str, Any],
    registered_tags: Mapping[str, Collection[str]],
    flag_lookup: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    """Admission-validate a whole plan, simulating the registry it builds.

    Entries are validated in order against a simulated child registry so
    the fleet cap, the iteration pool, and the mutating-tool overlap rule
    apply across the plan exactly as they will at spawn time. Raises
    :class:`~mission.validation.MissionValidationError` on the first
    failing entry (its ``details`` gain a ``plan_index``); returns the
    normalized, JSON-safe request list on success.
    """
    if not isinstance(entries, list) or not entries:
        raise MissionValidationError(
            "validation_error",
            details={"field": "plan", "reason": "empty_or_not_a_list"},
        )
    simulated: list[Any] = []
    sibling_allowlists: dict[str, list[str]] = {}
    requests: list[dict[str, Any]] = []
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise MissionValidationError(
                "validation_error",
                details={"field": "plan", "reason": "entry_not_a_dict", "plan_index": index},
            )
        try:
            spec = swarm_rules.validate_spawn(
                parent_role="orchestrator",
                config=config,
                children=simulated,
                request=entry,
                registered_tools=registered_tools,
                registered_tags=registered_tags,
                sibling_allowlists=sibling_allowlists,
                flag_lookup=flag_lookup,
            )
        except MissionValidationError as err:
            details = dict(err.details or {})
            details["plan_index"] = index
            raise MissionValidationError(err.code, details=details) from err
        simulated.append(
            swarm_rules.new_registry_entry(spec, f"plan-{index}", "1970-01-01T00:00:00+00:00")
        )
        sibling_allowlists[spec["slot"]] = list(spec["tool_allowlist"])
        requests.append(_spec_to_request(spec))
    return requests


def generate_deterministic_plan(
    directive: str,
    *,
    config: SwarmConfig,
    registered_tools: dict[str, Any],
    registered_tags: Mapping[str, Collection[str]],
    tool_allowlist: list[str] | None = None,
    allow_all_tools: bool = False,
    flag_lookup: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    """The always-available fallback: one child mirroring the directive.

    Criteria come from the deterministic criteria scaffold (so a
    search-flavoured directive with an allowlist gets concrete
    ``tool_call_succeeded`` entries), the budget is the module default
    bounded by the pool, and the restart policy is ``never``. The single
    entry runs through :func:`validate_plan` before returning, so the
    fallback can never emit a plan the spawn seam would reject.
    """
    iterations = min(DEFAULT_CHILD_MAX_ITERATIONS, config["child_iteration_pool"])
    entry: dict[str, Any] = {
        "slot": "worker-1",
        "directive": directive,
        "criteria": criteria_scaffold.generate_deterministic_criteria(
            directive, allowlist=tool_allowlist
        ),
        "budget": {
            "max_iterations": iterations,
            "max_wall_clock_seconds": DEFAULT_CHILD_MAX_WALL_CLOCK_SECONDS,
        },
        "restart_policy": "never",
        "use_sampling": False,
    }
    if allow_all_tools:
        entry["allow_all_tools"] = True
    else:
        entry["tool_allowlist"] = list(tool_allowlist or [])
    return validate_plan(
        [entry],
        config=config,
        registered_tools=registered_tools,
        registered_tags=registered_tags,
        flag_lookup=flag_lookup,
    )


def build_plan_prompt(
    directive: str,
    *,
    config: SwarmConfig,
    registered_tools: dict[str, Any],
    tool_docstrings: Mapping[str, str] | None = None,
    max_children: int | None = None,
    feedback: str | None = None,
) -> str:
    """Assemble the deterministic decomposition prompt.

    Deterministic given its inputs — tool names sorted, docstrings
    truncated to a fixed budget, the catalog capped — matching the
    byte-identity discipline of the wider sampling prompt builders.
    """
    cap = max_children if max_children is not None else config["max_children"]
    cap = max(1, min(cap, config["max_children"]))
    names = sorted(registered_tools)[:_PROMPT_TOOL_LIMIT]
    docs = tool_docstrings or {}
    catalog_lines = []
    for name in names:
        doc = str(docs.get(name, "")).strip().splitlines()
        first = doc[0][:_PROMPT_DOCSTRING_CHARS] if doc else ""
        catalog_lines.append(f"- {name}: {first}" if first else f"- {name}")
    sections = [
        "You are decomposing an operator goal into a fleet of supervised,",
        "budgeted worker sessions (a swarm plan). Respond with a single JSON",
        "array — no prose, no markdown fences. Each element is one child spec:",
        "",
        '{"slot": "<unique-name>", "directive": "<child goal>",',
        ' "criteria": [<mission criteria objects>],',
        ' "budget": {"max_iterations": <int >= 1>,',
        '            "max_wall_clock_seconds": <int >= 1>},',
        ' "tool_allowlist": ["<registered tool name>", ...],',
        ' "restart_policy": "never" | "on_failure" | "on_failure_with_revision"}',
        "",
        "Rules:",
        f"- At most {cap} children.",
        f"- The sum of max_iterations across children must not exceed "
        f"{config['child_iteration_pool']} (the shared iteration pool).",
        "- Budgets are finite: -1 is rejected on children.",
        "- Slot names: 1-64 chars, alphanumeric plus . _ - only.",
        "- Only tools from the catalog below may appear in an allowlist.",
        "- Two children must not share a non-read-only tool.",
        '- Criteria use metric paths like "metrics.<name>" and the kinds:',
        "  metric_threshold, metric_trend, event, tool_call_succeeded, predicate.",
        "",
        "=== Operator directive ===",
        directive,
        "",
        "=== Registered tool catalog ===",
        *catalog_lines,
    ]
    if feedback:
        sections.extend(["", "=== Validator feedback on your previous attempt ===", feedback])
    return "\n".join(sections)


class _PromptAdapter:
    """Thin ``SamplingPrompt`` look-alike over a pre-assembled string.

    Both shipped backends call ``prompt.assemble()`` and nothing else —
    the same duck-typing seam the criteria scaffolder and the
    semantic-progress judge exploit.
    """

    def __init__(self, text: str) -> None:
        self._text = text

    def assemble(self) -> str:
        return self._text


async def generate_sampled_plan(
    backend: Any,
    directive: str,
    *,
    config: SwarmConfig,
    registered_tools: dict[str, Any],
    registered_tags: Mapping[str, Collection[str]],
    tool_docstrings: Mapping[str, str] | None = None,
    max_children: int | None = None,
    retries: int = DEFAULT_PLAN_RETRIES,
    flag_lookup: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    """Drive a sampling backend to produce an admission-validated plan.

    Same loop discipline as ``generate_sampled_criteria``: attempt,
    parse, validate through :func:`validate_plan`, feed the precise
    rejection back, retry up to ``retries`` extra times, and raise
    :class:`SwarmScaffoldError` on exhaustion. Transport failures are
    not retriable here — the backend owns its own recovery — and
    surface as ``transport_error`` immediately.
    """
    feedback: str | None = None
    last_reason = "no_attempts"
    for _attempt in range(retries + 1):
        prompt = build_plan_prompt(
            directive,
            config=config,
            registered_tools=registered_tools,
            tool_docstrings=tool_docstrings,
            max_children=max_children,
            feedback=feedback,
        )
        try:
            raw = str(await backend.sample(_PromptAdapter(prompt)))
        except BedrockFTUFormNotAcceptedError:
            # A missing Anthropic first-time-use form is a permanent
            # account misconfiguration; report it rather than silently
            # downgrading to the deterministic plan (criteria-scaffold
            # precedent).
            raise
        except Exception as exc:  # noqa: BLE001 — transport-agnostic catch
            raise SwarmScaffoldError(
                "transport_error",
                message=f"sampling backend raised {type(exc).__name__}: {exc}",
            ) from exc
        try:
            parsed = _parse_json_array(raw)
        except ValueError as exc:
            last_reason = "json_parse"
            feedback = (
                "Your previous response could not be parsed as a JSON array. "
                f"Return a single JSON array, no prose, no markdown fences. ({exc})"
            )
            continue
        try:
            return validate_plan(
                parsed,
                config=config,
                registered_tools=registered_tools,
                registered_tags=registered_tags,
                flag_lookup=flag_lookup,
            )
        except MissionValidationError as exc:
            details = exc.details or {}
            last_reason = str(details.get("reason") or exc.code)
            feedback = (
                "Your previous response was rejected by the spawn validator. "
                f"Rejection reason: {last_reason}. Details: {details!r}. "
                "Re-emit a corrected JSON array."
            )
            continue
    raise SwarmScaffoldError(last_reason)


def _parse_json_array(raw: str) -> list[dict[str, Any]]:
    """Parse the model response as a JSON array, tolerating code fences."""
    text = raw.strip()
    if text.startswith("```"):
        lines = [line for line in text.splitlines() if not line.strip().startswith("```")]
        text = "\n".join(lines).strip()
    parsed = json.loads(text)
    if not isinstance(parsed, list):
        raise ValueError("expected a JSON array")
    return parsed


async def sample_revised_directive(
    backend: Any,
    failed_session: SessionState,
    *,
    lessons: list[str] | None = None,
) -> str | None:
    """Ask the backend for a revised directive after a child failure.

    Advisory text supply for ``on_failure_with_revision`` respawns —
    the deterministic restart table already made the respawn decision.
    Returns validated directive text, or ``None`` on any failure or
    unusable response so the caller falls back to the original
    directive. Never raises.
    """
    original = str(failed_session.get("directive_text", "")).strip()
    if not original:
        return None
    lesson_lines = [f"- {lesson}" for lesson in (lessons or []) if str(lesson).strip()]
    sections = [
        "A supervised worker session failed to meet its criteria and is being",
        "respawned. Revise its directive so the next attempt is more likely to",
        "succeed. Respond with exactly one line of plain text — the revised",
        "directive. No JSON, no prose around it.",
        "",
        "=== Original directive ===",
        original,
    ]
    if lesson_lines:
        sections.extend(["", "=== Lessons from the failed attempt ===", *lesson_lines])
    try:
        raw = str(await backend.sample(_PromptAdapter("\n".join(sections))))
        candidate = raw.strip().splitlines()[0].strip() if raw.strip() else ""
        return validate_directive(candidate)
    except Exception:  # noqa: BLE001 — advisory path degrades, never raises
        return None
