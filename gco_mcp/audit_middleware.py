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

from typing import Any

from audit import audit_elicitations_var, audit_messages_var
from fastmcp.server.context import Context
from fastmcp.server.middleware import Middleware

# Attribute we tag each spy function with so ``_install_context_patches``
# can detect that the wrappers are already in place. Reading an
# attribute on the live class method is more robust than a separate
# module-level boolean: if a re-import re-runs this module, the same
# marker survives because the patched method on ``Context`` survives.
_SPY_MARKER = "_gco_audit_spy"


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
        if lst is not None:
            lst.append({"level": "warning", "message": str(message)})
        return await _orig_warning(self, message, *args, **kwargs)

    async def _spy_info(self: Context, message: str, *args: Any, **kwargs: Any) -> Any:
        lst = audit_messages_var.get()
        if lst is not None:
            lst.append({"level": "info", "message": str(message)})
        return await _orig_info(self, message, *args, **kwargs)

    async def _spy_error(self: Context, message: str, *args: Any, **kwargs: Any) -> Any:
        lst = audit_messages_var.get()
        if lst is not None:
            lst.append({"level": "error", "message": str(message)})
        return await _orig_error(self, message, *args, **kwargs)

    async def _spy_elicit(self: Context, message: str, *args: Any, **kwargs: Any) -> Any:
        result = await _orig_elicit(self, message, *args, **kwargs)
        lst = audit_elicitations_var.get()
        if lst is not None:
            entry: dict[str, Any] = {
                "message": str(message),
                "action": getattr(result, "action", None),
            }
            data = getattr(result, "data", None)
            if data is not None:
                # Stringify to avoid leaking arbitrary user objects through
                # the audit log; the audit log is a JSON-line stream.
                entry["data"] = data if isinstance(data, (str, int, float, bool)) else str(data)
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


# Install patches eagerly at module import so callers that build their own
# pipelines (or tests that bypass middleware wiring) still get the capture
# behaviour as long as they install fresh ContextVars themselves.
_install_context_patches()
