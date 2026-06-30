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
- Startup audit log entry emitted at import time.
"""

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

_MAX_ARG_VALUE_BYTES = 1024  # 1KB

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


def _sanitize_arguments(kwargs: dict[str, Any]) -> dict[str, Any]:
    """Sanitize tool arguments for audit logging.

    - Redact values whose key name matches sensitive patterns (token, secret, password, key).
    - Truncate string values longer than 1KB to first 100 chars + '[truncated]'.
    - Replace values that aren't JSON-serializable (e.g. FastMCP ``Context``
      and ``Progress`` dependencies injected as keyword arguments) with a
      type-only placeholder so ``json.dumps(_build_audit_entry(...))`` can't
      raise ``TypeError`` mid-tool. Without this guard, every long-running
      tool that takes ``ctx``/``progress`` (deploy_all, destroy_all,
      bootstrap_cdk, deploy_stack, destroy_stack, images_build,
      images_push) crashes the wrapper with
      ``Object of type Context is not JSON serializable`` before the
      underlying CLI ever runs.
    """
    sanitized = {}
    for k, v in kwargs.items():
        # Check if the key name matches any sensitive pattern
        if any(pattern.match(k) for pattern in _SENSITIVE_KEY_PATTERNS):
            sanitized[k] = "[REDACTED]"
            continue

        # Truncate large string values
        str_val = str(v) if not isinstance(v, str) else v
        if len(str_val.encode("utf-8", errors="replace")) > _MAX_ARG_VALUE_BYTES:
            sanitized[k] = str_val[:100] + "[truncated]"
            continue

        # Probe JSON-serializability so injected dependencies (FastMCP
        # Context / Progress, dataclasses without ``default``, etc.) don't
        # blow up the audit emission. Bare primitives short-circuit the
        # try/except since ``json.dumps`` on str/int/float/bool/None/list/
        # dict-of-primitives is ~free.
        try:
            json.dumps(v)
            sanitized[k] = v
        except Exception:
            # Best-effort: any serialization failure (TypeError on unknown
            # types, ValueError on circular refs / NaN with allow_nan=False)
            # falls through to a type-only placeholder so the audit log
            # always emits valid JSON.
            sanitized[k] = f"<unserializable: {type(v).__name__}>"
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
    """Extract the FastMCP task ID from request meta when present.

    Every attribute access is wrapped in ``getattr(..., None)`` so a missing
    intermediate (no request_context, no meta, no task_id) yields ``None``
    rather than raising.
    """
    if ctx is None:
        return None
    rc = getattr(ctx, "request_context", None)
    if rc is None:
        return None
    meta = getattr(rc, "meta", None)
    if meta is None:
        return None
    return getattr(meta, "task_id", None)


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

    ctx = _try_get_fastmcp_context()
    if ctx is not None:
        # ``request_id`` raises if no request_context is set; guard with
        # request_context first so the access is safe.
        if getattr(ctx, "request_context", None) is not None:
            try:
                rid = ctx.request_id
            except Exception:
                rid = None
            if rid:
                entry["request_id"] = rid
        cid = getattr(ctx, "client_id", None)
        if cid:
            entry["client_id"] = cid
        tid = _try_get_task_id(ctx)
        if tid:
            entry["task_id"] = tid

    msgs = audit_messages_var.get()
    if msgs:
        entry["client_messages"] = list(msgs)
    elics = audit_elicitations_var.get()
    if elics:
        entry["elicitations"] = list(elics)

    return entry


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
