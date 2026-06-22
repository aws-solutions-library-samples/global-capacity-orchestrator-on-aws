"""Helpers for ``gco mission scaffold-criteria``.

The CLI subcommand turns a natural-language directive into a JSON
array of Criterion objects that ``mission.validation.validate_criteria``
accepts. Two paths are exposed:

* :func:`generate_deterministic_criteria` — pure, no I/O. Keyword-matches
  the directive against a small template table to pick a kind and shape
  the criterion. The default fallback is a single ``predicate`` with
  ``expression: "True"`` so the operator notices and edits before use.
  Always emits at most ``max_criteria`` entries.
* :func:`generate_sampled_criteria` — async, drives a resolved
  :class:`SamplingBackend` to produce JSON. The response is parsed,
  validated through ``validate_criteria``, and on rejection is retried
  up to ``retries`` times with a feedback prompt mentioning the
  rejection ``reason``. After the retry budget is exhausted, the helper
  raises :class:`ScaffoldSamplingError` so the caller can fall back to
  the deterministic path.
* :func:`build_scaffold_prompt` — render the prompt the sampling
  backend sees. Pure; lives here so tests can pin the exact text.

The module is import-light: no FastMCP, no boto3, no MCP server. It
imports the validators (and through them the predicate AST validator)
and the sampling Protocol type, but nothing that touches a transport.
The CLI wires the two paths together; this module keeps them
decoupled so each can be tested in isolation.
"""

from __future__ import annotations

import ast
import json
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from . import validation as _validation
from .predicate import PredicateRejected, parse_predicate
from .validation import MissionValidationError

# <pyflowchart-code-diagram> BEGIN - auto-inserted, do not edit
# Flowchart(s) generated from this file:
#   * ``generate_sampled_criteria`` -> ``diagrams/code_diagrams/gco_mcp/mission/criteria_scaffold.generate_sampled_criteria.html``
#     (PNG: ``diagrams/code_diagrams/gco_mcp/mission/criteria_scaffold.generate_sampled_criteria.png``)
# Regenerate with ``python diagrams/code_diagrams/generate.py``.
# <pyflowchart-code-diagram> END


if TYPE_CHECKING:  # pragma: no cover - type-checker only
    from .sampling import SamplingBackend


__all__ = [
    "DEFAULT_MAX_CRITERIA",
    "DEFAULT_RETRIES",
    "ScaffoldSamplingError",
    "build_scaffold_prompt",
    "generate_deterministic_criteria",
    "generate_sampled_criteria",
]


# ---------------------------------------------------------------------------
# Tunables
# ---------------------------------------------------------------------------

#: Default cap on the number of criteria scaffolded per call.
DEFAULT_MAX_CRITERIA: int = 5

#: Default retry count for the sampling path. Each retry re-prompts
#: the model with a feedback message containing the rejection reason.
DEFAULT_RETRIES: int = 3

# ---------------------------------------------------------------------------
# Keyword templates for the deterministic fallback
# ---------------------------------------------------------------------------

# Each entry is (regex, builder). The first match wins; builders
# return a single Criterion dict that ``validate_criteria`` accepts.
# The regex is matched case-insensitively against the directive.
# Order matters: more specific patterns appear first.

# "Lower is better" metrics (loss, error rate, latency, cost).
_LOWER_IS_BETTER_RE = re.compile(r"\b(loss|error|latency|cost)\b", re.IGNORECASE)

# "Higher is better" metrics (accuracy, throughput, recall, F1).
_HIGHER_IS_BETTER_RE = re.compile(r"\b(accuracy|throughput|f1|recall|precision)\b", re.IGNORECASE)

# Search-flavoured directives.
_SEARCH_RE = re.compile(r"\b(find|search|discover|locate|lookup)\b", re.IGNORECASE)

# Event-style directives.
_EVENT_RE = re.compile(
    r"\b(succeed|succeeded|complete|completed|finish|finished|emit)\b",
    re.IGNORECASE,
)


def _slugify(value: str, fallback: str = "criterion") -> str:
    """Turn a directive snippet into a stable criterion_id-friendly slug.

    Lowercase, ASCII letters / digits / underscores only. Empty input
    falls back to ``fallback``. Non-empty results are capped at 32
    chars so the audit log entries don't get unwieldy.
    """
    cleaned = re.sub(r"[^A-Za-z0-9]+", "_", value.strip().lower()).strip("_")
    if not cleaned:
        return fallback
    return cleaned[:32]


@dataclass(frozen=True)
class _DirectiveMatch:
    """Internal: a directive's matched template plus the captured token."""

    kind: str
    captured: str  # the matched keyword; informs slug + metric name


def _classify_directive(directive: str) -> _DirectiveMatch | None:
    """Pick the matching template for ``directive``, or ``None`` for default.

    The first match wins so more specific patterns can take precedence
    over the generic "search" template by listing first. Returns
    ``None`` when nothing matches; the caller then emits the
    placeholder predicate fallback.
    """
    if (m := _LOWER_IS_BETTER_RE.search(directive)) is not None:
        return _DirectiveMatch(kind="metric_threshold_lower", captured=m.group(1).lower())
    if (m := _HIGHER_IS_BETTER_RE.search(directive)) is not None:
        return _DirectiveMatch(kind="metric_threshold_higher", captured=m.group(1).lower())
    if _SEARCH_RE.search(directive) is not None:
        return _DirectiveMatch(kind="predicate_search", captured="search")
    if _EVENT_RE.search(directive) is not None:
        return _DirectiveMatch(kind="event", captured="job_succeeded")
    return None


def _build_metric_threshold(directive: str, captured: str, op: str) -> dict[str, Any]:
    """Build a ``metric_threshold`` criterion for the given keyword.

    The metric name uses ``val_<keyword>`` so it lines up with the
    common validation-loss / val-accuracy convention; the target is a
    placeholder the operator should override (0.1 for lower-is-better
    metrics, 0.9 for higher-is-better metrics).

    The dot-path is prefixed with ``metrics.`` because the engine's
    Observe_Phase merges the dispatcher's top-level ``metrics`` dict
    into the Observation under the ``metrics`` key, and the
    ``_evaluate_metric_threshold`` resolver walks the path against the
    Observation root. A bare ``val_loss`` (no prefix) would land on
    every iteration as ``inconclusive: metric_path_missing`` because
    the Observation's top level carries ``tool_results``, ``metrics``,
    ``events`` — not loose metric values. See
    :data:`tests.test_mission_e2e_train_to_loss` for the canonical
    end-to-end shape this prefix lines up with.
    """
    slug = _slugify(captured, fallback="metric")
    target = 0.1 if op in ("<", "<=") else 0.9
    metric_name = f"val_{captured}" if captured in ("loss", "accuracy") else captured
    return {
        "criterion_id": f"{slug}_target",
        "kind": "metric_threshold",
        "required": True,
        "metric": f"metrics.{metric_name}",
        "op": op,
        "target": target,
    }


def _build_predicate_search() -> dict[str, Any]:
    """The canonical search predicate: the iteration produced any results.

    Uses subscript form (``obs["tool_results"]``) rather than
    ``obs.get(...)`` because the predicate AST validator rejects
    method calls on ``obs`` — only the eight pure stdlib callables
    are allowed. Subscript notation is the documented surface for
    reading from the Observation.
    """
    return {
        "criterion_id": "results_present",
        "kind": "predicate",
        "required": True,
        "expression": "len(obs['tool_results']) > 0",
    }


def _build_tool_call_succeeded(tool_name: str) -> dict[str, Any]:
    """Build a ``tool_call_succeeded`` criterion targeting ``tool_name``.

    The slug is derived from the tool name so two ``tool_call_succeeded``
    entries in the same list don't collide on ``criterion_id``. The
    default ``min_count`` of 1 is left implicit on the criterion shape
    so the operator can edit it after scaffolding without first
    deleting an explicit value.
    """
    slug = _slugify(tool_name, fallback="tool")
    return {
        "criterion_id": f"{slug}_called",
        "kind": "tool_call_succeeded",
        "required": True,
        "tool_name": tool_name,
    }


def _build_event(captured: str) -> dict[str, Any]:
    """Build an ``event`` criterion using the captured keyword as the name."""
    return {
        "criterion_id": "expected_event",
        "kind": "event",
        "required": True,
        "event_name": captured,
    }


def _build_default_placeholder() -> dict[str, Any]:
    """Return the deterministic placeholder predicate.

    The expression is the literal ``True`` so the criterion is always
    met — this is intentional. The TODO note in the description is the
    cue for the operator to edit the file before running. Mission's
    validators accept the criterion as-is so the scaffolded output is
    always usable, but a session run with this criterion unmodified
    completes on iteration 0.
    """
    return {
        "criterion_id": "todo_placeholder",
        "kind": "predicate",
        "required": True,
        "expression": "True",
        # Non-required pass-through key (not on the validator's
        # required-keys list) so we don't trip schema validation.
        # It surfaces in the JSON for the operator to read.
        "description": "TODO: replace this placeholder with a real success condition.",
    }


def generate_deterministic_criteria(
    directive: str,
    *,
    allowlist: list[str] | None = None,
    max_criteria: int = DEFAULT_MAX_CRITERIA,
) -> list[dict[str, Any]]:
    """Build a criteria list deterministically from a directive.

    Always returns a list that ``validate_criteria`` accepts. The
    keyword-template lookup is naive on purpose — the fallback is
    *guidance for the operator*, not a substitute for thinking about
    the goal. The placeholder predicate is the explicit signal that
    no template matched.

    Args:
        directive: The natural-language goal.
        allowlist: Optional list of tool names. When the directive is
            a search-flavoured goal *and* an allowlist is supplied,
            the generator emits one ``tool_call_succeeded`` criterion
            per allowlisted tool (capped at ``max_criteria``) instead
            of the loose ``len(obs['tool_results']) > 0`` predicate.
            That gives the operator concrete per-tool success
            signals out of the box and keeps the criterion server-
            evaluated rather than going through the predicate AST
            sandbox. Falls back to the predicate when no allowlist
            is supplied so existing callers keep their shape.
        max_criteria: Cap on the number of entries returned. Always
            at least 1; values less than 1 are clamped.

    Returns:
        A list of one or more Criterion dicts. The list always
        validates through :func:`mission.validation.validate_criteria`.
    """
    if max_criteria < 1:
        max_criteria = 1
    match = _classify_directive(directive)
    if match is None:
        return [_build_default_placeholder()]
    if match.kind == "metric_threshold_lower":
        return [_build_metric_threshold(directive, match.captured, "<=")]
    if match.kind == "metric_threshold_higher":
        return [_build_metric_threshold(directive, match.captured, ">=")]
    if match.kind == "predicate_search":
        # Prefer per-tool ``tool_call_succeeded`` criteria when the
        # operator told us what tools they intend to allowlist —
        # those are server-evaluated and require zero predicate
        # syntax. Fall back to the loose predicate when no
        # allowlist is available so the no-allowlist call shape
        # stays exactly as it was.
        if allowlist:
            tool_names = list(allowlist)[:max_criteria]
            return [_build_tool_call_succeeded(name) for name in tool_names]
        return [_build_predicate_search()]
    if match.kind == "event":
        return [_build_event(match.captured)]
    # Defensive fallback — keeps mypy happy with the exhaustive return.
    return [_build_default_placeholder()]  # pragma: no cover


# ---------------------------------------------------------------------------
# Sampling path
# ---------------------------------------------------------------------------


class ScaffoldSamplingError(Exception):
    """Raised when every sampling attempt was rejected.

    The caller (the CLI) catches this and falls back to the
    deterministic path. The ``last_reason`` attribute carries the
    rejection token from the final retry so the CLI can surface it
    in a one-line warning.
    """

    def __init__(self, last_reason: str, message: str | None = None) -> None:
        self.last_reason: str = last_reason
        super().__init__(message or last_reason)


def build_scaffold_prompt(
    directive: str,
    *,
    allowlist: list[str] | None = None,
    max_criteria: int = DEFAULT_MAX_CRITERIA,
    feedback: str | None = None,
) -> str:
    """Render the prompt the sampling backend sees.

    The prompt asks for a strict JSON-array response, one entry per
    Criterion. The shape is described inline so the model doesn't
    need to fetch a schema document. The ``feedback`` argument carries
    the rejection reason from a prior attempt — when present, it is
    appended as a "feedback" block telling the model why the previous
    response was rejected.
    """
    allowlist_block = "(none specified)" if not allowlist else ", ".join(allowlist)
    sections: list[str] = []
    sections.append(
        "You are drafting Success_Criteria for a Mission goal-directed "
        "iteration loop. The operator's directive and the tool "
        "allowlist follow. Produce a JSON array of criterion objects "
        "the operator can hand to `gco mission start --criteria-file`."
    )
    sections.append("")
    sections.append("=== Directive ===")
    sections.append(directive)
    sections.append("")
    sections.append("=== Tool allowlist ===")
    sections.append(allowlist_block)
    sections.append("")
    sections.append(f"=== Cap: at most {max_criteria} criterion entries ===")
    sections.append("")
    sections.append("=== Observation shape (read by predicates and metric paths) ===")
    sections.append(
        "Each iteration's Observation is a dict with these fields:\n"
        '  - "tool_results": list[dict] — every tool the iteration\n'
        "    called returns one entry. Each entry is whatever the\n"
        "    tool itself returned, plus a top-level ``_status`` flag.\n"
        '  - "metrics": dict[str, Any] — numeric / scalar values\n'
        "    surfaced by tools that emit them. The dot-path for a\n"
        "    metric_threshold criterion against ``val_loss`` is\n"
        '    ``"metrics.val_loss"`` (NOT ``"val_loss"``); the engine\n'
        "    walks the path against the Observation root and a bare\n"
        "    name will land as ``inconclusive: metric_path_missing``\n"
        "    on every iteration.\n"
        '  - "events": list[dict] — emitted events, each with an\n'
        "    ``event_name`` key.\n"
        '  - "errors" (optional): list[dict] — errors any tool raised.\n'
        '  - "phase_started_at" / "phase_ended_at": ISO-8601 strings.'
    )
    sections.append("")
    sections.append("=== Output schema ===")
    sections.append(
        "Return a single JSON array. Each entry is an object with "
        "these required keys:\n"
        '  - "criterion_id": unique non-empty string\n'
        '  - "kind": one of "metric_threshold" / "event" / '
        '"predicate" / "tool_call_succeeded"\n'
        '  - "required": JSON boolean\n'
        "Plus the kind-specific keys:\n"
        '  metric_threshold     -> "metric" (DOT-PATH into the\n'
        "                          Observation, e.g.\n"
        "                          ``metrics.val_loss``,\n"
        "                          ``tool_results.0.score``), "
        '"op" (one of <, <=, >, >=, ==, !=), "target" (number)\n'
        '  event                -> "event_name" (non-empty string;\n'
        '                          matched against entries in obs["events"])\n'
        '  tool_call_succeeded  -> "tool_name" (non-empty string;\n'
        "                          matched against entries in\n"
        '                          ``obs["tool_results"]`` whose\n'
        '                          ``_status`` equals ``"ok"``).\n'
        '                          Optional: "min_count" (positive\n'
        "                          int, default 1).\n"
        "                          PREFER this kind over a predicate\n"
        '                          when the goal is "this tool ran\n'
        '                          and succeeded" — it is server-\n'
        "                          evaluated and never goes through\n"
        "                          the predicate AST sandbox.\n"
        '  predicate            -> "expression" (a Python expression\n'
        "                          evaluated against `obs` — see\n"
        "                          the predicate vocabulary section\n"
        "                          below for the exact surface)"
    )
    sections.append("")
    sections.append("=== Predicate vocabulary ===")
    sections.append(
        "Predicate expressions run inside a tight AST sandbox. The\n"
        "allowed surface:\n"
        "\n"
        "Names: ``obs`` (the Observation dict).\n"
        "Top-level callables (twelve, all pure stdlib):\n"
        "  ``len``, ``min``, ``max``, ``sum``, ``abs``,\n"
        "  ``any``, ``all``, ``sorted``,\n"
        "  ``str``, ``int``, ``float``, ``bool`` (type coercions).\n"
        "Read-only method calls on any value (seven, all pure):\n"
        "  ``.get(key[, default])``, ``.keys()``, ``.values()``,\n"
        "  ``.items()``, ``.lower()``, ``.upper()``, ``.strip()``\n"
        "Operators: arithmetic, comparisons (<, <=, >, >=, ==, !=,\n"
        "  is, is not, in, not in), boolean (and, or, not), ternary\n"
        "  (a if b else c).\n"
        "Containers: list/tuple/dict/set literals, list / set / dict\n"
        "  / generator comprehensions (the comprehension target may\n"
        "  not shadow ``obs`` or any callable name).\n"
        "Subscripts: ``obs['key']``, ``obs['k']['nested']``,\n"
        "  ``obs['list'][0]``, etc.\n"
        "Attribute access: ONLY single-level on ``obs`` (e.g. ``obs.events``\n"
        "  for read-only access; subscript form is preferred). Nested\n"
        "  walks like ``obs.a.b`` are rejected — use ``obs['a']['b']``.\n"
        "\n"
        "Method calls outside the seven pure-accessor names are\n"
        "rejected (no ``.append``, ``.update``, ``.pop``, ``.count``,\n"
        "``.startswith``, ``.split``, etc.). Calls to non-allowlisted\n"
        "names (``list``, ``dict``, ``getattr``, ``isinstance``, ...)\n"
        "are rejected."
    )
    sections.append("")
    sections.append("=== Predicate examples (do NOT use rejected forms) ===")
    sections.append(
        "ACCEPTED predicate expressions:\n"
        "  len(obs['tool_results']) > 0\n"
        "  obs['metrics']['val_loss'] < 0.1\n"
        "  any(e['event_name'] == 'goal_reached' for e in obs['events'])\n"
        "  any(r.get('_status') == 'ok' for r in obs['tool_results'])\n"
        "  all(r.get('_status') == 'ok' for r in obs['tool_results'])\n"
        "  any(r.get('_status') == 'ok' and r.get('tool_name') == 'find_docs'\n"
        "      for r in obs['tool_results'])\n"
        "  any('inference' in str(r).lower() for r in obs['tool_results'])\n"
        "  len(obs.get('errors', [])) == 0\n"
        "  any(k == 'val_loss' for k in obs['metrics'].keys())\n"
        "\n"
        "REJECTED predicate expressions (will fail validation):\n"
        "  obs.metrics.val_loss < 0.1       # nested attribute walk; use obs['metrics']['val_loss']\n"  # noqa: E501
        "  obs['tool_results'].count('ok')  # ``.count`` is not on the method allowlist\n"
        "  obs['tool_results'].append(1)    # ``.append`` mutates and is not allowed\n"
        "  any(r.split(',') for r in obs['tool_results'])  # ``.split`` not on method allowlist\n"
        "  any(k.startswith('val_') for k in obs['metrics'].keys())  # ``.startswith`` not on method allowlist\n"  # noqa: E501
        "  getattr(obs, 'tool_results')     # ``getattr`` not on callable allowlist\n"
        "  obs['x'].y.z                     # attribute walk after subscript"
    )
    sections.append("")
    sections.append("Output only the JSON array. No prose, no markdown fences.")
    if feedback:
        sections.append("")
        sections.append("=== Feedback on previous attempt ===")
        sections.append(feedback)
    return "\n".join(sections)


def _parse_response(text: str) -> list[dict[str, Any]]:
    """Extract a JSON array from a model response.

    Models occasionally wrap JSON in markdown fences; tolerate that.
    Raises ``ValueError`` when no JSON array is recoverable.
    """
    stripped = text.strip()
    # Strip markdown fences if present.
    if stripped.startswith("```"):
        # remove first fence line
        first_newline = stripped.find("\n")
        if first_newline != -1:
            stripped = stripped[first_newline + 1 :]
        if stripped.endswith("```"):
            stripped = stripped[:-3].rstrip()
    # Find the first '[' and last ']' so we tolerate trailing prose.
    start = stripped.find("[")
    end = stripped.rfind("]")
    if start == -1 or end == -1 or end < start:
        raise ValueError("no JSON array found in response")
    parsed = json.loads(stripped[start : end + 1])
    if not isinstance(parsed, list):
        raise ValueError("JSON payload is not a list")
    out: list[dict[str, Any]] = []
    for entry in parsed:
        if not isinstance(entry, dict):
            raise ValueError("array entry is not an object")
        out.append(entry)
    return out


def _normalize_kind_name(criterion: dict[str, Any]) -> dict[str, Any]:
    """Rewrite obvious ``kind`` typos to the canonical names.

    Models occasionally emit a near-miss for the criterion ``kind``
    field — pluralising (``tool_calls_succeeded`` instead of the
    canonical ``tool_call_succeeded``), abbreviating
    (``threshold`` instead of ``metric_threshold``), or hyphenating
    (``tool-call-succeeded`` instead of underscore form). The
    structural validator rejects these with ``kind_invalid`` and the
    retry-with-feedback path can recover, but the typos are
    mechanical: a closed alias map covers every captured emission
    we have seen across Bedrock models.

    The map is intentionally narrow — we only canonicalise a name
    when it is unambiguously a typo for one of the four valid
    kinds, never a name a future kind extension might claim. Returns
    the input unchanged when the kind is already canonical, missing,
    or not a string. Returns a shallow copy when a rewrite fires so
    the input dict is never mutated.
    """
    kind = criterion.get("kind")
    if not isinstance(kind, str):
        return criterion
    canonical = _KIND_ALIASES.get(kind)
    if canonical is None:
        return criterion
    if canonical == kind:
        return criterion
    out = dict(criterion)
    out["kind"] = canonical
    return out


# Closed alias map for ``_normalize_kind_name``. Every entry here was
# observed in the captured fixture corpus under
# ``tests/fixtures/scaffold_responses/`` — adding a new entry is the
# right move only when a captured model emits a near-miss the
# rejection-feedback retry doesn't recover on the next attempt.
_KIND_ALIASES: dict[str, str] = {
    # Llama 4 Scout pluralises the kind name in its first emission.
    "tool_calls_succeeded": "tool_call_succeeded",
    # Hyphenated forms occasionally surface from JSON-schema-trained
    # smaller models that map ``snake_case`` onto ``kebab-case``.
    "tool-call-succeeded": "tool_call_succeeded",
    "metric-threshold": "metric_threshold",
}


def _normalize_metric_path(criterion: dict[str, Any]) -> dict[str, Any]:
    """Auto-prefix bare metric names with ``metrics.`` for ``metric_threshold``.

    The engine's metric path resolver walks the dot-path against the
    Observation root, where canonical metric values live under the
    ``metrics`` sub-dict. A bare ``"val_loss"`` lands as
    ``inconclusive: metric_path_missing`` on every iteration.

    Models trained on generic metric semantics tend to emit bare
    names anyway. Rather than reject the response and burn a retry,
    this normaliser injects the ``metrics.`` prefix when:

    1. ``kind == "metric_threshold"``,
    2. ``metric`` is a non-empty string,
    3. The string contains no ``.`` separator (so already-qualified
       paths like ``tool_results.0.score`` or
       ``metrics.something.nested`` pass through verbatim).

    Returns a shallow copy so the input is never mutated. The strip is
    idempotent on already-prefixed values: ``"metrics.foo"`` has a
    ``.`` so it falls through unchanged.
    """
    if criterion.get("kind") != "metric_threshold":
        return criterion
    metric = criterion.get("metric")
    if not isinstance(metric, str) or not metric:
        return criterion
    if "." in metric:
        return criterion
    out = dict(criterion)
    out["metric"] = f"metrics.{metric}"
    return out


class _AttributeToSubscriptRewriter(ast.NodeTransformer):
    """Rewrite ``obs.<attr>`` chains as ``obs['<attr>']`` chains.

    The predicate validator accepts a single-level attribute read on
    ``obs`` (``obs.tool_results``) but rejects nested attribute walks
    (``obs.metrics.val_loss``) and method-style calls
    (``obs.x.any()``, ``obs.get('x')``). Models routinely emit those
    shapes because they are the obvious Pythonic idioms. This
    transformer rewrites the *attribute-walk* shapes mechanically;
    method-call shapes that need creative rewriting are left alone so
    the standard retry-with-feedback loop can teach the model.

    The walk only rewrites attribute reads whose innermost base is the
    ``Name('obs')`` — every other attribute access (e.g. on a list
    element returned from a comprehension, on a number) is left
    untouched so the validator's other guards still apply.
    """

    def visit_Attribute(self, node: ast.Attribute) -> ast.AST:  # noqa: N802 - ast hook name
        # Recurse into the value first so a nested attribute walk gets
        # rewritten bottom-up: ``obs.metrics.val_loss`` -> visit
        # ``obs.metrics`` first (which becomes ``obs['metrics']``)
        # then wrap the result in ``[...]['val_loss']``.
        self.generic_visit(node)
        # Only rewrite when the rewritten base is one of:
        #   * Name('obs') — the simple ``obs.x`` case
        #   * Subscript whose ultimate base is Name('obs') — the
        #     already-rewritten ``obs['metrics']`` case
        # Anything else (attribute on a Call, on a list literal, on a
        # comprehension target) is left as-is so the validator's
        # rejections still fire on shapes the autofix shouldn't try to
        # silently rescue.
        base = node.value
        innermost = base
        while isinstance(innermost, ast.Subscript):
            innermost = innermost.value
        if not (isinstance(innermost, ast.Name) and innermost.id == "obs"):
            return node
        return ast.Subscript(
            value=base,
            slice=ast.Constant(value=node.attr),
            ctx=node.ctx,
        )


def _autofix_predicate(criterion: dict[str, Any]) -> dict[str, Any]:
    """Best-effort rewrite of attribute-walk predicates into subscript form.

    Keeps the crit dict unchanged when:

    * ``kind != "predicate"``
    * the expression is missing or non-string
    * the expression already parses cleanly through
      :func:`mission.predicate.parse_predicate`
    * source has a syntax error (the validator will reject it with the
      original code anyway)
    * the rewritten expression *still* fails validation (so the
      retry-with-feedback path runs against the original source the
      model emitted, not a partially-rewritten one)

    Returns a shallow copy with the rewritten ``expression`` only when
    the rewrite produced a predicate that clears the validator. This
    mirrors :func:`_normalize_metric_path` — never mutates input,
    always returns a JSON-safe dict.
    """
    if criterion.get("kind") != "predicate":
        return criterion
    expression = criterion.get("expression")
    if not isinstance(expression, str) or not expression:
        return criterion
    # Cheap fast path: if the source is already valid, don't pay the
    # cost of an AST round-trip on the happy case.
    try:
        parse_predicate(expression)
        return criterion
    except PredicateRejected:
        pass

    try:
        tree = ast.parse(expression, mode="eval")
    except SyntaxError:
        return criterion

    rewritten_tree = _AttributeToSubscriptRewriter().visit(tree)
    ast.fix_missing_locations(rewritten_tree)
    try:
        rewritten_src = ast.unparse(rewritten_tree)
    except Exception:  # noqa: BLE001 - unparse failure leaves us no better off
        return criterion

    # Re-validate the rewrite. If the rewrite still doesn't validate
    # (e.g. a method call like ``obs.x.any()`` produced
    # ``obs['x'].any()`` which is still a method-call-on-subscript),
    # fall back to the original so the retry-with-feedback loop sees
    # the model's actual emission.
    try:
        parse_predicate(rewritten_src)
    except PredicateRejected:
        return criterion

    out = dict(criterion)
    out["expression"] = rewritten_src
    return out


async def generate_sampled_criteria(
    backend: SamplingBackend,
    directive: str,
    *,
    allowlist: list[str] | None = None,
    max_criteria: int = DEFAULT_MAX_CRITERIA,
    retries: int = DEFAULT_RETRIES,
) -> list[dict[str, Any]]:
    """Drive a sampling backend to produce a validated criteria list.

    Builds the prompt, calls ``backend.sample(prompt_str)``, parses
    the JSON, validates through :func:`validate_criteria`, and on
    rejection retries up to ``retries`` times with feedback. Returns
    the validated list (with private ``_parsed_ast`` keys stripped so
    the result is JSON-safe). Raises :class:`ScaffoldSamplingError`
    when every attempt was rejected.

    The backend is duck-typed against the ``SamplingBackend`` protocol
    on purpose — tests can substitute a stub object whose ``sample``
    method returns canned strings without bringing in a transport.
    """
    feedback: str | None = None
    last_reason = "no_attempts"
    # We do retries + 1 total attempts — the first attempt is "free",
    # then each retry is one extra try.
    for attempt in range(retries + 1):
        prompt_str = build_scaffold_prompt(
            directive,
            allowlist=allowlist,
            max_criteria=max_criteria,
            feedback=feedback,
        )
        try:
            raw = await _call_backend(backend, prompt_str)
        except Exception as exc:  # noqa: BLE001 - transport-agnostic catch
            # Transport-layer failures are not retriable from the
            # scaffolder's point of view — the backend itself decides
            # whether to recover. Surface as a sampling error so the
            # CLI falls back deterministically.
            raise ScaffoldSamplingError(
                "transport_error",
                message=f"sampling backend raised {type(exc).__name__}: {exc}",
            ) from exc
        try:
            parsed = _parse_response(raw)
        except (ValueError, json.JSONDecodeError) as exc:
            last_reason = "json_parse"
            feedback = (
                "Your previous response could not be parsed as a JSON "
                "array. Return a single JSON array, no prose, no "
                f"markdown fences. ({exc})"
            )
            continue
        # Cap to max_criteria — if the model returned more, truncate
        # rather than rejecting outright. The structural validator
        # below catches everything else.
        if len(parsed) > max_criteria:
            parsed = parsed[:max_criteria]
        # Best-effort kind-name normalisation runs first so the
        # metric-path / predicate-autofix passes branch correctly on
        # the canonical ``kind``. Models occasionally pluralise
        # (``tool_calls_succeeded``) or hyphenate
        # (``tool-call-succeeded``) the kind name; rewriting to the
        # canonical form here saves a retry round-trip. The map is
        # closed and explicit — see ``_KIND_ALIASES``.
        parsed = [_normalize_kind_name(c) for c in parsed]
        # Best-effort normalisation: a model that emits a bare metric
        # name (``"val_loss"``) instead of the dot-path
        # (``"metrics.val_loss"``) the engine actually walks would
        # otherwise produce a session whose metric_threshold criterion
        # silently evaluates ``inconclusive: metric_path_missing`` on
        # every iteration. The prompt now teaches this convention but
        # we still post-process for robustness against older prompts
        # and models that ignore the schema.
        parsed = [_normalize_metric_path(c) for c in parsed]
        # Best-effort autofix for predicate expressions: the predicate
        # AST validator rejects attribute-walk patterns
        # (``obs.metrics.val_loss``) and method-call shapes
        # (``obs.get('x')``, ``obs.x.any()``) — both are common Python
        # idioms the model defaults to. The rewriter rescues the
        # attribute-walk shape into subscript notation; method-call
        # shapes that need creative rewriting fall through to the
        # standard retry-with-feedback path so the model gets the
        # rejection token and tries again.
        parsed = [_autofix_predicate(c) for c in parsed]
        try:
            validated = _validation.validate_criteria(parsed)
        except MissionValidationError as exc:
            details = exc.details or {}
            last_reason = str(details.get("reason") or exc.code)
            feedback = (
                "Your previous response was rejected by the validator. "
                f"Rejection reason: {last_reason}. Details: {details!r}. "
                "Re-emit a corrected JSON array."
            )
            continue
        # Strip private cached AST keys so the JSON written to disk is
        # round-trippable. ``_parsed_ast`` is attached to predicate
        # entries by ``validate_criteria``.
        del attempt
        return [
            {k: v for k, v in entry.items() if not str(k).startswith("_")} for entry in validated
        ]
    raise ScaffoldSamplingError(last_reason)


async def _call_backend(backend: SamplingBackend, prompt_str: str) -> str:
    """Adapt the protocol's ``sample(SamplingPrompt)`` call to a string prompt.

    The :class:`SamplingBackend` protocol takes a structured
    :class:`SamplingPrompt`. The criteria-scaffold use case is a
    one-off prompt rather than a full Mission round-trip, so we
    construct a minimal ``SamplingPrompt`` whose render produces
    exactly ``prompt_str``. Backends that need extra context
    (Bedrock's region, MCP's model preferences) read from their bound
    state and ignore the prompt's surrounding fields.

    Tests can substitute a stub backend whose ``sample`` returns a
    canned string; those tests pass the stub directly to
    :func:`generate_sampled_criteria` and bypass the protocol entirely.
    """
    # Lazy import to avoid the import cycle: sampling imports validation
    # which would otherwise import this module.
    from .sampling import SamplingPrompt  # noqa: PLC0415

    # Wrap the prompt string in a dataclass that renders to itself.
    # The full SamplingPrompt has many required fields; the scaffolder
    # uses a thin adapter that overrides ``assemble`` so the existing
    # backend implementations call ``assemble()`` and get the prompt.
    prompt_obj = _PromptAdapter(prompt_str)
    # Backends accept any object with an ``assemble`` method. Both
    # MCPSamplingBackend and BedrockSamplingBackend call
    # ``prompt.assemble()`` to get the rendered string.
    del SamplingPrompt  # imported only for documentation linkage
    return await backend.sample(prompt_obj)  # type: ignore[arg-type]


class _PromptAdapter:
    """Minimal duck-typed stand-in for :class:`SamplingPrompt`.

    Both backends call ``prompt.assemble()`` to render the prompt
    string. This adapter satisfies that single contract so the
    scaffolder can route a free-form prompt through the same backend
    surface the engine uses, without constructing a full
    SamplingPrompt with iteration history that does not exist for a
    one-off scaffolding call.
    """

    def __init__(self, text: str) -> None:
        self._text: str = text

    def assemble(self) -> str:
        return self._text
