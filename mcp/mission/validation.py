"""Shared validators for Mission session inputs.

Every Mission entry point — the MCP tools, the CLI subcommands, the engine's
session loader — feeds operator-supplied JSON through this module before it
ever touches state. The validators are intentionally pure: no I/O, no clocks,
no environment lookups. The caller passes whatever external context is
needed (the FastMCP tool catalog, the per-tool tag sets, the gating
feature-flag lookup) as plain arguments. This keeps the validators trivial
to unit-test and makes them safe to call from both async tool handlers and
synchronous CLI code.

Design notes:

* Validators **return new normalized values**; they never mutate their
  inputs. The :func:`validate_criteria` case attaches a cached parsed AST
  under the private key ``_parsed_ast`` on each ``predicate`` criterion;
  the original input dict is left untouched and a shallow copy carries the
  added key.

* Every rejection raises :class:`MissionValidationError` with a stable
  short ``code`` (e.g. ``"validation_error"``) and a structured
  ``details`` dict whose ``field`` key identifies the input that failed.
  Tool wrappers render ``code`` and ``details`` as a structured FastMCP
  tool error so clients can surface them without text parsing.

* The script-strategy path forward-declares the sandbox: scripted
  strategies are out of scope for this module and the sandbox module
  lands in a later slice. The lazy import inside
  :func:`validate_strategy` tolerates the missing module by raising a
  dedicated ``script_sandbox_not_implemented`` code, so callers that hit
  this path get a clear signal rather than an ``ImportError`` traceback.
"""

from __future__ import annotations

from typing import Any, Final, cast

from . import predicate
from .types import (
    BudgetControls,
    Cadence,
    Criterion,
    Strategy,
)

# ---------------------------------------------------------------------------
# Public exception
# ---------------------------------------------------------------------------


class MissionValidationError(Exception):
    """Raised when a validator rejects an input.

    Carries a stable short ``code`` and an optional structured ``details``
    dict. FastMCP tool wrappers convert this into a structured tool-error
    response; CLI handlers print ``code`` plus the ``details`` JSON.

    The constructor accepts ``(code, details=None, *, message=None)``.
    When ``message`` is not provided, the exception's string form falls
    back to ``code`` so logs always show something meaningful.
    """

    def __init__(
        self,
        code: str,
        details: dict[str, Any] | None = None,
        *,
        message: str | None = None,
    ) -> None:
        self.code: str = code
        self.details: dict[str, Any] | None = details
        rendered = message if message is not None else code
        super().__init__(rendered)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_DIRECTIVE_MAX_LEN: Final[int] = 8192
"""The hard cap on directive_text length, in characters."""

_COST_TAGS: Final[frozenset[str]] = frozenset(
    {"cost-incurring", "data-upload", "image", "infrastructure"}
)
"""Tool tag set that forces ``max_cost_usd`` to be present on the budget.

Any registered tool whose tag set intersects this set is considered
cost-incurring; a session whose tool_allowlist includes such a tool must
declare ``max_cost_usd`` so the engine has a hard cap to enforce.
"""

_CRITERION_KINDS: Final[frozenset[str]] = frozenset({"metric_threshold", "event", "predicate"})
"""The three valid Criterion ``kind`` values."""

_METRIC_OPS: Final[frozenset[str]] = frozenset({"<", "<=", ">", ">=", "==", "!="})
"""The six valid comparison operators on a ``metric_threshold`` criterion."""

_CADENCE_KINDS: Final[frozenset[str]] = frozenset(
    {"every_iteration", "every_n_iterations", "every_t_seconds", "on_event"}
)
"""The four valid Cadence ``kind`` values."""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _is_positive_int(value: Any) -> bool:
    """Return True iff ``value`` is an int (not bool) and strictly > 0."""
    # bool is a subclass of int; reject it explicitly so True/False cannot
    # silently masquerade as a positive integer count.
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _is_number(value: Any) -> bool:
    """Return True iff ``value`` is an int or float (not bool)."""
    return isinstance(value, (int, float)) and not isinstance(value, bool)


# ---------------------------------------------------------------------------
# Directive
# ---------------------------------------------------------------------------


def validate_directive(text: str) -> str:
    """Trim and validate a directive string.

    The directive is the operator-supplied natural-language goal. It must
    be a non-empty string (after stripping leading/trailing whitespace)
    and must fit within :data:`_DIRECTIVE_MAX_LEN` characters. Returns
    the trimmed string. Raises :class:`MissionValidationError` with
    ``code="validation_error"`` on rejection.
    """
    if not isinstance(text, str):
        raise MissionValidationError(
            "validation_error",
            details={"field": "directive", "reason": "not_a_string"},
        )
    trimmed = text.strip()
    if not trimmed:
        raise MissionValidationError(
            "validation_error",
            details={"field": "directive", "reason": "empty"},
        )
    if len(trimmed) > _DIRECTIVE_MAX_LEN:
        raise MissionValidationError(
            "validation_error",
            details={
                "field": "directive",
                "reason": "too_long",
                "max_length": _DIRECTIVE_MAX_LEN,
                "actual_length": len(trimmed),
            },
        )
    return trimmed


# ---------------------------------------------------------------------------
# Criteria
# ---------------------------------------------------------------------------


def _validate_metric_threshold(entry: dict[str, Any], criterion_id: str) -> None:
    """Check the kind-specific keys for a ``metric_threshold`` criterion."""
    metric = entry.get("metric")
    if not isinstance(metric, str) or not metric:
        raise MissionValidationError(
            "validation_error",
            details={
                "field": "criteria",
                "criterion_id": criterion_id,
                "reason": "metric_missing_or_invalid",
            },
        )
    op = entry.get("op")
    if op not in _METRIC_OPS:
        raise MissionValidationError(
            "validation_error",
            details={
                "field": "criteria",
                "criterion_id": criterion_id,
                "reason": "op_invalid",
                "allowed": sorted(_METRIC_OPS),
            },
        )
    target = entry.get("target")
    if not _is_number(target):
        raise MissionValidationError(
            "validation_error",
            details={
                "field": "criteria",
                "criterion_id": criterion_id,
                "reason": "target_not_a_number",
            },
        )


def _validate_event_criterion(entry: dict[str, Any], criterion_id: str) -> None:
    """Check the kind-specific keys for an ``event`` criterion."""
    event_name = entry.get("event_name")
    if not isinstance(event_name, str) or not event_name:
        raise MissionValidationError(
            "validation_error",
            details={
                "field": "criteria",
                "criterion_id": criterion_id,
                "reason": "event_name_missing_or_invalid",
            },
        )


def _validate_predicate_criterion(entry: dict[str, Any], criterion_id: str) -> Any:
    """Check the kind-specific keys for a ``predicate`` criterion.

    Returns the parsed AST so the caller can attach it under
    ``_parsed_ast`` on the normalized copy.
    """
    expression = entry.get("expression")
    if not isinstance(expression, str) or not expression:
        raise MissionValidationError(
            "validation_error",
            details={
                "field": "criteria",
                "criterion_id": criterion_id,
                "reason": "expression_missing_or_invalid",
            },
        )
    try:
        return predicate.parse_predicate(expression)
    except predicate.PredicateRejected as exc:
        raise MissionValidationError(
            "validation_error",
            details={
                "field": "criteria",
                "criterion_id": criterion_id,
                "reason": exc.reason,
                "lineno": exc.lineno,
                "col_offset": exc.col_offset,
            },
        ) from exc


def validate_criteria(criteria: list[dict[str, Any]]) -> list[Criterion]:
    """Validate a list of criteria and attach cached predicate ASTs.

    Required keys on every entry: ``criterion_id`` (non-empty str),
    ``kind`` (one of the three :class:`CriterionKind` values), and
    ``required`` (bool). Each entry must also provide the kind-specific
    keys: ``metric``/``op``/``target`` for ``metric_threshold``,
    ``event_name`` for ``event``, ``expression`` for ``predicate``.

    The ``criterion_id`` must be unique across the list. For each
    ``predicate`` entry, the expression is parsed via
    :func:`predicate.parse_predicate` and the resulting AST is cached
    under the private key ``_parsed_ast`` on a shallow copy of the
    entry. Returns the normalized list. The original input dicts are
    not mutated.
    """
    if not isinstance(criteria, list):
        raise MissionValidationError(
            "validation_error",
            details={"field": "criteria", "reason": "not_a_list"},
        )
    if not criteria:
        raise MissionValidationError(
            "validation_error",
            details={"field": "criteria", "reason": "empty"},
        )
    seen_ids: set[str] = set()
    normalized: list[Criterion] = []
    for index, entry in enumerate(criteria):
        if not isinstance(entry, dict):
            raise MissionValidationError(
                "validation_error",
                details={
                    "field": "criteria",
                    "index": index,
                    "reason": "not_a_dict",
                },
            )
        criterion_id = entry.get("criterion_id")
        if not isinstance(criterion_id, str) or not criterion_id:
            raise MissionValidationError(
                "validation_error",
                details={
                    "field": "criteria",
                    "index": index,
                    "reason": "criterion_id_missing_or_invalid",
                },
            )
        if criterion_id in seen_ids:
            raise MissionValidationError(
                "validation_error",
                details={
                    "field": "criteria",
                    "criterion_id": criterion_id,
                    "reason": "duplicate_criterion_id",
                },
            )
        seen_ids.add(criterion_id)
        kind = entry.get("kind")
        if kind not in _CRITERION_KINDS:
            raise MissionValidationError(
                "validation_error",
                details={
                    "field": "criteria",
                    "criterion_id": criterion_id,
                    "reason": "kind_invalid",
                    "allowed": sorted(_CRITERION_KINDS),
                },
            )
        if not isinstance(entry.get("required"), bool):
            raise MissionValidationError(
                "validation_error",
                details={
                    "field": "criteria",
                    "criterion_id": criterion_id,
                    "reason": "required_missing_or_not_a_bool",
                },
            )
        # Build a shallow copy so we never mutate the caller's dict; we
        # may need to attach _parsed_ast and we want the input to stay
        # exactly as it was passed in.
        normalized_entry: dict[str, Any] = dict(entry)
        if kind == "metric_threshold":
            _validate_metric_threshold(entry, criterion_id)
        elif kind == "event":
            _validate_event_criterion(entry, criterion_id)
        else:  # kind == "predicate"
            parsed = _validate_predicate_criterion(entry, criterion_id)
            normalized_entry["_parsed_ast"] = parsed
        normalized.append(cast("Criterion", normalized_entry))
    return normalized


# ---------------------------------------------------------------------------
# Budget
# ---------------------------------------------------------------------------


def _allowlist_includes_cost_tool(
    allowlist: list[str], registered_tags: dict[str, set[str]]
) -> bool:
    """True iff any tool in the allowlist carries a cost-incurring tag."""
    for tool_name in allowlist:
        tags = registered_tags.get(tool_name)
        if tags is not None and tags & _COST_TAGS:
            return True
    return False


def validate_budget(
    budget: dict[str, Any],
    allowlist: list[str],
    registered_tags: dict[str, set[str]],
) -> BudgetControls:
    """Validate a budget dict against the allowlist's tag profile.

    Required keys: ``max_iterations`` and ``max_wall_clock_seconds``,
    both positive ints. ``max_cost_usd`` is required only when the
    ``allowlist`` contains at least one tool whose registered tag set
    (looked up in ``registered_tags``) intersects :data:`_COST_TAGS`.
    When required and present, ``max_cost_usd`` must be a positive
    number. Returns a normalized dict suitable for use as a
    :class:`BudgetControls`.
    """
    if not isinstance(budget, dict):
        raise MissionValidationError(
            "validation_error",
            details={"field": "budget", "reason": "not_a_dict"},
        )
    max_iterations = budget.get("max_iterations")
    if not _is_positive_int(max_iterations):
        raise MissionValidationError(
            "validation_error",
            details={
                "field": "budget",
                "subfield": "max_iterations",
                "reason": "missing_or_not_positive_int",
            },
        )
    max_wall = budget.get("max_wall_clock_seconds")
    if not _is_positive_int(max_wall):
        raise MissionValidationError(
            "validation_error",
            details={
                "field": "budget",
                "subfield": "max_wall_clock_seconds",
                "reason": "missing_or_not_positive_int",
            },
        )
    cost_required = _allowlist_includes_cost_tool(allowlist, registered_tags)
    normalized: dict[str, Any] = {
        "max_iterations": max_iterations,
        "max_wall_clock_seconds": max_wall,
    }
    if "max_cost_usd" in budget:
        max_cost = budget["max_cost_usd"]
        if not (_is_number(max_cost) and max_cost > 0):
            raise MissionValidationError(
                "validation_error",
                details={
                    "field": "budget",
                    "subfield": "max_cost_usd",
                    "reason": "not_a_positive_number",
                },
            )
        normalized["max_cost_usd"] = max_cost
    elif cost_required:
        raise MissionValidationError(
            "validation_error",
            details={
                "field": "budget",
                "subfield": "max_cost_usd",
                "reason": "required_for_cost_incurring_allowlist",
                "cost_tags": sorted(_COST_TAGS),
            },
        )
    return cast("BudgetControls", normalized)


# ---------------------------------------------------------------------------
# Tool allowlist
# ---------------------------------------------------------------------------


def validate_tool_allowlist(
    allowlist: list[str],
    registered_tools: dict[str, Any],
    flag_lookup: dict[str, str] | None = None,
) -> list[str]:
    """Validate that every name in the allowlist is currently registered.

    ``registered_tools`` is a structural mapping from tool name to the
    tool object (FastMCP's ``Tool`` type, but typed loosely here so the
    module imports cleanly without the optional FastMCP dependency).
    Only the dict keys are read.

    When a name is missing from ``registered_tools``, the validator
    raises :class:`MissionValidationError`. If ``flag_lookup`` is
    provided and contains the missing tool's name, the rejection's
    ``details.flag`` field carries the gating feature-flag name (so
    the operator can be told *why* the tool is currently absent —
    typically because its feature flag is unset). Otherwise the
    rejection carries ``details.tool_name`` only.
    """
    if not isinstance(allowlist, list):
        raise MissionValidationError(
            "validation_error",
            details={"field": "tool_allowlist", "reason": "not_a_list"},
        )
    if not allowlist:
        raise MissionValidationError(
            "validation_error",
            details={"field": "tool_allowlist", "reason": "empty"},
        )
    seen: set[str] = set()
    normalized: list[str] = []
    for index, name in enumerate(allowlist):
        if not isinstance(name, str) or not name:
            raise MissionValidationError(
                "validation_error",
                details={
                    "field": "tool_allowlist",
                    "index": index,
                    "reason": "tool_name_missing_or_invalid",
                },
            )
        if name in seen:
            raise MissionValidationError(
                "validation_error",
                details={
                    "field": "tool_allowlist",
                    "tool_name": name,
                    "reason": "duplicate_tool_name",
                },
            )
        seen.add(name)
        if name not in registered_tools:
            details: dict[str, Any] = {
                "field": "tool_allowlist",
                "tool_name": name,
                "reason": "tool_not_registered",
            }
            if flag_lookup is not None and name in flag_lookup:
                details["flag"] = flag_lookup[name]
            raise MissionValidationError("validation_error", details=details)
        normalized.append(name)
    return normalized


# ---------------------------------------------------------------------------
# Cadence
# ---------------------------------------------------------------------------


def validate_cadence(cadence: dict[str, Any]) -> Cadence:
    """Validate a checkpoint cadence dict.

    The base ``every_iteration`` kind requires no extra keys.
    ``every_n_iterations`` requires a positive int ``n``.
    ``every_t_seconds`` requires a positive int ``t``. ``on_event``
    requires a non-empty str ``event_name``. Returns a normalized dict
    suitable for use as a :class:`Cadence`.
    """
    if not isinstance(cadence, dict):
        raise MissionValidationError(
            "validation_error",
            details={"field": "checkpoint_cadence", "reason": "not_a_dict"},
        )
    kind = cadence.get("kind")
    if kind not in _CADENCE_KINDS:
        raise MissionValidationError(
            "validation_error",
            details={
                "field": "checkpoint_cadence",
                "reason": "kind_invalid",
                "allowed": sorted(_CADENCE_KINDS),
            },
        )
    normalized: dict[str, Any] = {"kind": kind}
    if kind == "every_n_iterations":
        n = cadence.get("n")
        if not _is_positive_int(n):
            raise MissionValidationError(
                "validation_error",
                details={
                    "field": "checkpoint_cadence",
                    "subfield": "n",
                    "reason": "missing_or_not_positive_int",
                },
            )
        normalized["n"] = n
    elif kind == "every_t_seconds":
        t = cadence.get("t")
        if not _is_positive_int(t):
            raise MissionValidationError(
                "validation_error",
                details={
                    "field": "checkpoint_cadence",
                    "subfield": "t",
                    "reason": "missing_or_not_positive_int",
                },
            )
        normalized["t"] = t
    elif kind == "on_event":
        event_name = cadence.get("event_name")
        if not isinstance(event_name, str) or not event_name:
            raise MissionValidationError(
                "validation_error",
                details={
                    "field": "checkpoint_cadence",
                    "subfield": "event_name",
                    "reason": "missing_or_empty",
                },
            )
        normalized["event_name"] = event_name
    # every_iteration takes no extra keys; nothing else to copy.
    return cast("Cadence", normalized)


# ---------------------------------------------------------------------------
# Strategy
# ---------------------------------------------------------------------------


def validate_strategy(
    strategy: dict[str, Any],
    allowlist: list[str],
    allow_scripts: bool,
) -> Strategy:
    """Validate a Propose_Phase Strategy dict.

    Exactly one of ``tool_calls`` (a non-empty list) or ``script`` (a
    non-empty string) must be present. When ``script`` is present,
    ``allow_scripts`` must be ``True`` — sessions started with
    ``allow_scripted_strategies=False`` reject scripted proposals. The
    script is then handed to the sandbox AST validator
    (:func:`mission.sandbox.validate_script_ast`) for inspection
    against ``allowlist``. The sandbox module is imported lazily
    because it lands in a later slice; if it is missing at call time,
    :class:`MissionValidationError` is raised with the dedicated code
    ``script_sandbox_not_implemented`` so callers see a clear signal
    instead of an ``ImportError`` traceback.

    Returns a normalized strategy dict carrying through the optional
    ``expected_observation_keys`` and ``rationale`` fields when
    present.
    """
    if not isinstance(strategy, dict):
        raise MissionValidationError(
            "validation_error",
            details={"field": "strategy", "reason": "not_a_dict"},
        )
    has_tool_calls = "tool_calls" in strategy
    has_script = "script" in strategy
    if has_tool_calls == has_script:
        # Both present, or both absent — same error in either direction.
        raise MissionValidationError(
            "validation_error",
            details={
                "field": "strategy",
                "reason": "must_have_exactly_one_of_tool_calls_or_script",
            },
        )

    normalized: dict[str, Any] = {}
    if has_tool_calls:
        tool_calls = strategy["tool_calls"]
        if not isinstance(tool_calls, list) or not tool_calls:
            raise MissionValidationError(
                "validation_error",
                details={
                    "field": "strategy",
                    "subfield": "tool_calls",
                    "reason": "must_be_non_empty_list",
                },
            )
        # Shallow-copy each call dict so the caller's list/dicts stay
        # intact; we don't impose a deep schema on each call here
        # because the tool dispatcher validates the per-call args
        # against the registered tool's signature at execute time.
        normalized["tool_calls"] = [dict(call) for call in tool_calls]
    else:
        script = strategy["script"]
        if not isinstance(script, str) or not script:
            raise MissionValidationError(
                "validation_error",
                details={
                    "field": "strategy",
                    "subfield": "script",
                    "reason": "must_be_non_empty_string",
                },
            )
        if not allow_scripts:
            raise MissionValidationError(
                "validation_error",
                details={
                    "field": "strategy",
                    "subfield": "script",
                    "reason": "scripts_not_allowed_by_session",
                },
            )
        try:
            from mission.sandbox import (  # noqa: PLC0415 — lazy: sandbox is an optional runtime dep
                ScriptRejected,
                validate_script_ast,
            )
        except ModuleNotFoundError as exc:
            raise MissionValidationError(
                "script_sandbox_not_implemented",
                details={
                    "hint": "scripted strategies require the sandbox module",
                },
            ) from exc
        try:
            validate_script_ast(script, allowlist)
        except ScriptRejected as exc:
            # Translate the sandbox-level rejection into our structured
            # MissionValidationError so every operator-input rejection
            # comes back through the same exception type. The sandbox's
            # stable ``reason`` token, line, and column carry through
            # so callers can render a precise error.
            raise MissionValidationError(
                "validation_error",
                details={
                    "field": "strategy",
                    "subfield": "script",
                    "reason": exc.reason,
                    "lineno": exc.lineno,
                    "col_offset": exc.col_offset,
                },
            ) from exc
        normalized["script"] = script

    # Carry through the two optional pass-through fields when present.
    if "expected_observation_keys" in strategy:
        keys = strategy["expected_observation_keys"]
        if not isinstance(keys, list) or not all(isinstance(k, str) for k in keys):
            raise MissionValidationError(
                "validation_error",
                details={
                    "field": "strategy",
                    "subfield": "expected_observation_keys",
                    "reason": "must_be_list_of_strings",
                },
            )
        normalized["expected_observation_keys"] = list(keys)
    if "rationale" in strategy:
        rationale = strategy["rationale"]
        if not isinstance(rationale, str):
            raise MissionValidationError(
                "validation_error",
                details={
                    "field": "strategy",
                    "subfield": "rationale",
                    "reason": "not_a_string",
                },
            )
        normalized["rationale"] = rationale
    return cast("Strategy", normalized)
