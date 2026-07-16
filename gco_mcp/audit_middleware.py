"""
Audit-capture middleware for the GCO MCP server.

Wires up two pieces:

1. ``Context.{warning, info, error, elicit}`` are wrapped at the class level
   (once, at module import time) so every Context instance — including the
   fresh one FastMCP creates for each tool call — appends to the active
   capture buffers.
2. ``AuditCaptureMiddleware`` sets fresh capture buffers in
   ``audit_messages_var`` / ``audit_elicitations_var`` at the start of every
   ``on_call_tool`` invocation and resets them on the way out. The audit
   decorator (``gco_mcp/audit.py::_build_audit_entry``) reads those buffers
   when emitting the entry.

The Context class only has its methods patched once. Idempotency is
enforced by inspecting an attribute we set on the patched function;
re-imports (test reloads, hot-reload during dev) detect the marker
and skip re-patching. The wrapped methods short-circuit to the
originals when no capture buffer is active, so this patch is a no-op
for any code that uses Context outside of a tool call (e.g. unit
tests that construct a Context directly).
"""

from __future__ import annotations

import time
from typing import Any

from audit import (
    _sanitize_value,
    _truncate_string,
    audit_elicitations_var,
    audit_messages_var,
    audit_resource_read,
)
from fastmcp.server.context import Context
from fastmcp.server.middleware import Middleware

# Attribute we tag each spy function with so ``_install_context_patches``
# can detect that the wrappers are already in place. Reading an
# attribute on the live class method is more robust than a separate
# module-level boolean: if a re-import re-runs this module, the same
# marker survives because the patched method on ``Context`` survives.
_SPY_MARKER = "_gco_audit_spy"
_MAX_CAPTURED_MESSAGES = 100
_MAX_CAPTURED_ELICITATIONS = 100


def _install_context_patches() -> None:
    """Install the class-level Context method wrappers (once)."""
    if getattr(Context.warning, _SPY_MARKER, False):
        return

    _orig_warning = Context.warning
    _orig_info = Context.info
    _orig_error = Context.error
    _orig_elicit = Context.elicit

    async def _spy_warning(self: Context, message: str, *args: Any, **kwargs: Any) -> Any:
        lst = audit_messages_var.get()
        if lst is not None and len(lst) < _MAX_CAPTURED_MESSAGES:
            lst.append({"level": "warning", "message": _truncate_string(str(message))})
        return await _orig_warning(self, message, *args, **kwargs)

    async def _spy_info(self: Context, message: str, *args: Any, **kwargs: Any) -> Any:
        lst = audit_messages_var.get()
        if lst is not None and len(lst) < _MAX_CAPTURED_MESSAGES:
            lst.append({"level": "info", "message": _truncate_string(str(message))})
        return await _orig_info(self, message, *args, **kwargs)

    async def _spy_error(self: Context, message: str, *args: Any, **kwargs: Any) -> Any:
        lst = audit_messages_var.get()
        if lst is not None and len(lst) < _MAX_CAPTURED_MESSAGES:
            lst.append({"level": "error", "message": _truncate_string(str(message))})
        return await _orig_error(self, message, *args, **kwargs)

    async def _spy_elicit(self: Context, message: str, *args: Any, **kwargs: Any) -> Any:
        result = await _orig_elicit(self, message, *args, **kwargs)
        lst = audit_elicitations_var.get()
        if lst is not None and len(lst) < _MAX_CAPTURED_ELICITATIONS:
            entry: dict[str, Any] = {
                "message": _truncate_string(str(message)),
                "action": _sanitize_value(getattr(result, "action", None), depth=0, seen=set()),
            }
            data = getattr(result, "data", None)
            if data is not None:
                entry["data"] = _sanitize_value(data, depth=0, seen=set())
            lst.append(entry)
        return result

    # Tag each spy with the marker before attaching so a concurrent
    # re-entry observes the marker as soon as the assignment lands.
    for spy in (_spy_warning, _spy_info, _spy_error, _spy_elicit):
        setattr(spy, _SPY_MARKER, True)

    Context.warning = _spy_warning  # type: ignore[method-assign]
    Context.info = _spy_info  # type: ignore[method-assign]
    Context.error = _spy_error  # type: ignore[method-assign]
    Context.elicit = _spy_elicit  # type: ignore[method-assign]


class AuditCaptureMiddleware(Middleware):
    """FastMCP middleware that activates per-invocation audit capture buffers.

    On every ``on_call_tool`` call, sets fresh empty lists into
    ``audit_messages_var`` and ``audit_elicitations_var`` so the patched
    Context methods append into them. Resets the ContextVars on the way
    out so concurrent calls don't see each other's captures.
    """

    def __init__(self) -> None:
        _install_context_patches()

    async def on_call_tool(self, context: Any, call_next: Any) -> Any:
        messages: list[dict[str, str]] = []
        elicitations: list[dict[str, object]] = []
        msg_token = audit_messages_var.set(messages)
        elic_token = audit_elicitations_var.set(elicitations)
        try:
            return await call_next(context)
        finally:
            audit_messages_var.reset(msg_token)
            audit_elicitations_var.reset(elic_token)

    async def on_read_resource(self, context: Any, call_next: Any) -> Any:
        """Audit every static and templated resource read in one place."""
        started = time.time()
        uri = getattr(getattr(context, "message", None), "uri", "")
        fastmcp_context = getattr(context, "fastmcp_context", None)
        try:
            result = await call_next(context)
        except Exception as exc:
            audit_resource_read(
                uri,
                status="error",
                duration_ms=(time.time() - started) * 1000,
                error=str(exc),
                ctx=fastmcp_context,
            )
            raise
        audit_resource_read(
            uri,
            status="success",
            duration_ms=(time.time() - started) * 1000,
            ctx=fastmcp_context,
        )
        return result


# Install patches eagerly at module import so callers that build their own
# pipelines (or tests that bypass middleware wiring) still get the capture
# behaviour as long as they install fresh ContextVars themselves.
_install_context_patches()
