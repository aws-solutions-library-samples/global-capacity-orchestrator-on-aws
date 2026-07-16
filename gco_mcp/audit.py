"""
Audit logging infrastructure for the GCO MCP server.

Provides:
- ``_sanitize_arguments`` — redacts sensitive keys, truncates large values.
- ``audit_logged`` — decorator that emits structured JSON audit entries for
  every MCP tool invocation (success or failure). Dispatches on
  ``inspect.iscoroutinefunction`` so async tools work transparently.
- ``audit_messages_var`` / ``audit_elicitations_var`` — ContextVars populated
  by ``gco_mcp/audit_middleware.py`` to surface ``ctx.warning``/``info``/``error``
  /``elicit`` calls in the audit entry.
- ``audit_resource_read`` — emits the same structured success/error metadata
  for every MCP resource read via centralized middleware.
- Startup audit log entry emitted only when the server entry point starts.
"""

import contextlib
import contextvars
import functools
import inspect
import json
import logging
import os
import re
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import feature_flags
from version import get_project_version

# <pyflowchart-code-diagram> BEGIN - auto-inserted, do not edit
# Flowchart(s) generated from this file:
#   * ``audit_logged`` -> ``diagrams/code_diagrams/gco_mcp/audit.audit_logged.html``
#     (PNG: ``diagrams/code_diagrams/gco_mcp/audit.audit_logged.png``)
# Regenerate with ``python diagrams/code_diagrams/generate.py``.
# <pyflowchart-code-diagram> END


# =============================================================================
# AUDIT LOGGING
# =============================================================================

_MCP_SERVER_VERSION = get_project_version()

audit_logger = logging.getLogger("gco.mcp.audit")

# Patterns for sensitive argument key names (case-insensitive)
_SENSITIVE_KEY_PATTERNS = [
    re.compile(r".*token.*", re.IGNORECASE),
    re.compile(r".*secret.*", re.IGNORECASE),
    re.compile(r".*password.*", re.IGNORECASE),
    re.compile(r".*key.*", re.IGNORECASE),
]

_MAX_ARG_VALUE_BYTES = 1024  # 1KB per string leaf
_MAX_TASK_ID_BYTES = 256
_MAX_AUDIT_DEPTH = 12
_MAX_CONTAINER_ITEMS = 100
_CIRCULAR_VALUE = "<circular-reference>"
_MAX_DEPTH_VALUE = "<max-depth-exceeded>"

# Per-invocation capture buffers populated by the audit middleware. The
# middleware sets fresh lists at the start of every tool call; the audit
# decorator reads them at the end and includes them in the entry when
# non-empty. Default ``None`` means "no capture in scope" — the patched
# Context methods short-circuit to the originals without recording.
audit_messages_var: contextvars.ContextVar[list[dict[str, str]] | None] = contextvars.ContextVar(
    "gco_audit_messages", default=None
)
audit_elicitations_var: contextvars.ContextVar[list[dict[str, object]] | None] = (
    contextvars.ContextVar("gco_audit_elicitations", default=None)
)


def _is_sensitive_key(key: object) -> bool:
    """Return whether a mapping key names a value that must be redacted."""
    return isinstance(key, str) and any(pattern.match(key) for pattern in _SENSITIVE_KEY_PATTERNS)


def _truncate_string(value: str) -> str:
    """Bound one string leaf by encoded byte length."""
    if len(value.encode("utf-8", errors="replace")) <= _MAX_ARG_VALUE_BYTES:
        return value
    return value[:100] + "[truncated]"


def _truncate_task_id(value: str) -> str:
    """Bound an audit task identifier without assuming an ASCII-only value."""
    encoded = value.encode("utf-8", errors="replace")
    if len(encoded) <= _MAX_TASK_ID_BYTES:
        return value
    marker = b"[truncated]"
    prefix = encoded[: _MAX_TASK_ID_BYTES - len(marker)]
    return prefix.decode("utf-8", errors="ignore") + marker.decode()


def _sanitize_value(value: Any, *, depth: int, seen: set[int]) -> Any:
    """Recursively redact and bound one audit value without calling user ``repr``.

    Mapping keys are inspected at every depth before their values are visited,
    preventing a nested secret from leaking through a parent container's string
    representation. Containers are depth/item bounded and cycle-aware so audit
    logging cannot become an unbounded traversal of attacker-controlled input.
    """
    if isinstance(value, str):
        return _truncate_string(value)
    if value is None or isinstance(value, (bool, int, float)):
        try:
            json.dumps(value, allow_nan=False)
            return value
        except TypeError, ValueError:
            return f"<unserializable: {type(value).__name__}>"
    if depth >= _MAX_AUDIT_DEPTH:
        return _MAX_DEPTH_VALUE

    if isinstance(value, dict):
        identity = id(value)
        if identity in seen:
            return _CIRCULAR_VALUE
        seen.add(identity)
        try:
            sanitized: dict[Any, Any] = {}
            for index, (key, nested) in enumerate(value.items()):
                if index >= _MAX_CONTAINER_ITEMS:
                    break
                safe_key: Any = key
                if not isinstance(key, (str, int, float, bool)) and key is not None:
                    safe_key = f"<key:{type(key).__name__}>"
                sanitized[safe_key] = (
                    "[REDACTED]"
                    if _is_sensitive_key(key)
                    else _sanitize_value(nested, depth=depth + 1, seen=seen)
                )
            if len(value) > _MAX_CONTAINER_ITEMS:
                sanitized["<truncated-items>"] = len(value) - _MAX_CONTAINER_ITEMS
            return sanitized
        finally:
            seen.remove(identity)

    if isinstance(value, (list, tuple)):
        identity = id(value)
        if identity in seen:
            return _CIRCULAR_VALUE
        seen.add(identity)
        try:
            sanitized_items = [
                _sanitize_value(item, depth=depth + 1, seen=seen)
                for item in value[:_MAX_CONTAINER_ITEMS]
            ]
            if len(value) > _MAX_CONTAINER_ITEMS:
                sanitized_items.append(f"<truncated-items:{len(value) - _MAX_CONTAINER_ITEMS}>")
            return sanitized_items
        finally:
            seen.remove(identity)

    # Unknown objects are intentionally not coerced through str/repr: those
    # methods can expose credentials or execute arbitrary user code.
    return f"<unserializable: {type(value).__name__}>"


def _sanitize_arguments(kwargs: dict[str, Any]) -> dict[str, Any]:
    """Recursively sanitize tool arguments for audit logging.

    Sensitive mapping keys are redacted at every nesting depth. String leaves,
    container depth, and container item counts are bounded. Unknown objects are
    represented by type only, keeping audit emission JSON-safe without invoking
    potentially secret-bearing ``__str__`` implementations.
    """
    sanitized: dict[str, Any] = {}
    seen: set[int] = {id(kwargs)}
    for key, value in kwargs.items():
        sanitized[key] = (
            "[REDACTED]" if _is_sensitive_key(key) else _sanitize_value(value, depth=0, seen=seen)
        )
    return sanitized


def _try_get_fastmcp_context() -> Any | None:
    """Return the active FastMCP Context if inside a request, else None.

    Wrapping the import lets ``audit_logged`` work in unit tests that don't
    go through an MCP request — ``get_context()`` raises ``RuntimeError`` in
    that case, which we swallow.
    """
    try:
        from fastmcp.server.dependencies import get_context

        return get_context()
    except Exception:
        return None


def _try_get_task_id(ctx: Any | None) -> str | None:
    """Extract the active FastMCP task ID through supported APIs first.

    Docket-backed FastMCP workers expose the protocol task through
    ``get_task_context()`` rather than request metadata. ``Context.task_id`` is
    the next supported surface; the metadata walk remains as a compatibility
    fallback for older FastMCP releases and focused callers. Any identifier is
    byte-bounded before it can enter an audit record or task-status decision.
    """
    candidates: list[object] = []
    try:
        from fastmcp.server.dependencies import get_task_context

        candidates.append(getattr(get_task_context(), "task_id", None))
    except Exception:
        pass

    if ctx is not None:
        with contextlib.suppress(Exception):
            candidates.append(getattr(ctx, "task_id", None))
        with contextlib.suppress(Exception):
            request_context = getattr(ctx, "request_context", None)
            meta = getattr(request_context, "meta", None) if request_context is not None else None
            candidates.append(getattr(meta, "task_id", None) if meta is not None else None)

    for task_id in candidates:
        if isinstance(task_id, str) and task_id:
            return _truncate_task_id(task_id)
    return None


def _add_request_context_fields(entry: dict[str, Any], ctx: Any | None = None) -> None:
    """Add common request/client/task identifiers to one audit entry."""
    ctx = _try_get_fastmcp_context() if ctx is None else ctx
    if ctx is None:
        return
    try:
        request_context = getattr(ctx, "request_context", None)
        request_id = getattr(ctx, "request_id", None) if request_context is not None else None
        client_id = getattr(ctx, "client_id", None)
    except Exception:
        request_id = None
        client_id = None
    if request_id:
        entry["request_id"] = request_id
    if client_id:
        entry["client_id"] = client_id
    task_id = _try_get_task_id(ctx)
    if task_id:
        entry["task_id"] = task_id


def _build_audit_entry(
    func_name: str,
    sanitized_args: dict[str, Any],
    status: str,
    duration_ms: float,
    error: str | None,
    result: Any,  # noqa: ARG001  -- reserved for future result-shape capture
) -> dict[str, Any]:
    """Build the JSON dict for a single tool-invocation audit entry.

    Optional fields (``error``, ``request_id``, ``client_id``, ``task_id``,
    ``client_messages``, ``elicitations``) are omitted when their values
    are missing or empty. Existing sync-tool entries that don't trigger
    any new field look identical to the pre-refactor shape.
    """
    entry: dict[str, Any] = {
        "event": "mcp.tool.invocation",
        "tool": func_name,
        "arguments": sanitized_args,
        "status": status,
        "duration_ms": round(duration_ms, 2),
        "timestamp": datetime.now(UTC).isoformat(),
    }
    if error:
        entry["error"] = error[:200]

    _add_request_context_fields(entry)

    msgs = audit_messages_var.get()
    if msgs:
        entry["client_messages"] = list(msgs)
    elics = audit_elicitations_var.get()
    if elics:
        entry["elicitations"] = list(elics)

    return entry


def audit_resource_read(
    resource_uri: object,
    *,
    status: str,
    duration_ms: float,
    error: str | None = None,
    ctx: Any | None = None,
) -> None:
    """Emit one bounded audit record for an MCP resource read."""
    entry: dict[str, Any] = {
        "event": "mcp.resource.read",
        "resource_uri": _truncate_string(str(resource_uri)),
        "status": status,
        "duration_ms": round(duration_ms, 2),
        "timestamp": datetime.now(UTC).isoformat(),
    }
    if error:
        entry["error"] = _truncate_string(error)[:200]
    _add_request_context_fields(entry, ctx)
    audit_logger.info(json.dumps(entry))


def audit_logged(func: Callable[..., Any]) -> Callable[..., Any]:
    """Decorator that emits structured JSON audit entries for tool invocations.

    Dispatches on ``inspect.iscoroutinefunction(func)``: async tools get an
    async wrapper that ``await``s the call, sync tools keep the existing
    sync path. Both wrappers share ``_build_audit_entry``.
    """
    if inspect.iscoroutinefunction(func):

        @functools.wraps(func)
        async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
            start = time.time()
            sanitized_args = _sanitize_arguments(kwargs)
            try:
                result = await func(*args, **kwargs)
                duration_ms = (time.time() - start) * 1000
                audit_logger.info(
                    json.dumps(
                        _build_audit_entry(
                            func.__name__,
                            sanitized_args,
                            "success",
                            duration_ms,
                            None,
                            result,
                        )
                    )
                )
                return result
            except Exception as e:
                duration_ms = (time.time() - start) * 1000
                audit_logger.info(
                    json.dumps(
                        _build_audit_entry(
                            func.__name__,
                            sanitized_args,
                            "error",
                            duration_ms,
                            str(e),
                            None,
                        )
                    )
                )
                raise

        return async_wrapper

    @functools.wraps(func)
    def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
        start = time.time()
        sanitized_args = _sanitize_arguments(kwargs)
        try:
            result = func(*args, **kwargs)
            duration_ms = (time.time() - start) * 1000
            audit_logger.info(
                json.dumps(
                    _build_audit_entry(
                        func.__name__,
                        sanitized_args,
                        "success",
                        duration_ms,
                        None,
                        result,
                    )
                )
            )
            return result
        except Exception as e:
            duration_ms = (time.time() - start) * 1000
            audit_logger.info(
                json.dumps(
                    _build_audit_entry(
                        func.__name__,
                        sanitized_args,
                        "error",
                        duration_ms,
                        str(e),
                        None,
                    )
                )
            )
            raise

    return sync_wrapper


# =============================================================================
# STARTUP LOG
# =============================================================================

# Recognised values for the ``GCO_MCP_TOOL_SEARCH`` env var. Anything outside
# this set normalises to ``"bm25"`` — the same fallback rule that
# ``gco_mcp/server.py`` uses when wiring the catalog-replacement transform.
_TOOL_SEARCH_VALUES = ("bm25", "regex", "code_mode", "off")


def _resolve_tool_search() -> str:
    """Return the effective ``GCO_MCP_TOOL_SEARCH`` value after normalisation.

    Mirrors the resolution in ``gco_mcp/server.py``: read the env var, strip and
    lowercase, then fall back to ``"bm25"`` for unset, empty, or unknown
    values so the audit entry reports what was actually wired.
    """
    raw = os.environ.get("GCO_MCP_TOOL_SEARCH", "bm25").strip().lower()
    return raw if raw in _TOOL_SEARCH_VALUES else "bm25"


def emit_startup_log() -> None:
    """Emit the startup audit log entry."""
    entry: dict[str, Any] = {
        "event": "mcp.server.startup",
        "version": _MCP_SERVER_VERSION,
        "audit_log_level": logging.getLevelName(audit_logger.getEffectiveLevel()),
        "timestamp": datetime.now(UTC).isoformat(),
    }
    if feature_flags.all_tools_enabled():
        entry["all_tools_enabled"] = True
    if feature_flags.is_enabled(feature_flags.FLAG_MISSION):
        entry["mission_enabled"] = True
    # Every per-tool flag that is effectively enabled (by its own env var or by
    # the umbrella), so an audit consumer sees the full gated-tool surface for a
    # run from a single line instead of diffing env vars. Sorted for stable
    # output and omitted entirely when nothing beyond the default-on set is
    # enabled. The all_tools_enabled / mission_enabled booleans above are kept
    # for backward compatibility with existing audit consumers.
    enabled_flags = sorted(f for f in feature_flags.ALL_FLAGS if feature_flags.is_enabled(f))
    if enabled_flags:
        entry["enabled_flags"] = enabled_flags
    tool_search = _resolve_tool_search()
    entry["tool_search"] = tool_search
    if tool_search == "code_mode":
        entry["code_mode_experimental"] = True
    audit_logger.info(json.dumps(entry))
