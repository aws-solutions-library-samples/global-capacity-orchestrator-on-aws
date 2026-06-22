"""Mission session resources (``mission://sessions/{session_id}`` and ``.../report``).

[gated by GCO_ENABLE_MISSION]

Two resource templates that surface live Mission_Session state and the
durable Final_Report artifact through the FastMCP resource layer:

* ``mission://sessions/{session_id}`` — returns the JSON-serialised live
  session payload. Returns a JSON error envelope when the session is
  unknown so that tool-only clients (which call the synthetic
  ``read_resource`` tool produced by the Resources As Tools transform)
  receive a stable string body rather than a transport-level error.
* ``mission://sessions/{session_id}/report`` — returns the Final_Report
  JSON for a terminal session. Raises a not-found error when the
  session is missing, not yet terminal, or its report has not been
  written; FastMCP maps that exception to the MCP ``-32002 Resource
  not found`` code on the wire.

Both templates are gated by :data:`feature_flags.FLAG_MISSION` at
registration time. When the flag is unset, :func:`register` is a no-op
so importing this module from ``resources/__init__.py`` is always safe.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from feature_flags import FLAG_MISSION, is_enabled

# Mission package lives under ``gco_mcp/mission/``; the path-injection
# pattern matches the rest of the MCP module surface so ``import
# mission.*`` resolves without making the ``mcp`` directory a package.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _strip_private_fields(session: Mapping[str, Any]) -> dict[str, Any]:
    """Return a JSON-safe copy of ``session`` with private criterion keys dropped.

    Thin alias over :func:`mission.validation.strip_private_fields` —
    the canonical implementation lives next to ``validate_criteria``
    (which creates the ``_parsed_ast`` keys) so a single function
    governs the JSON-safety contract across CLI, MCP tools, and MCP
    resources.
    """
    from mission.validation import strip_private_fields

    return strip_private_fields(session)


def _make_not_found(message: str) -> Exception:
    """Construct the best available "not found" exception for a missing resource.

    Prefers :class:`fastmcp.exceptions.NotFoundError` because the
    FastMCP error-handling middleware maps it (along with
    :class:`KeyError` and :class:`FileNotFoundError`) to MCP error code
    ``-32002`` when the method namespace is ``resources/``. Falls back
    to :class:`fastmcp.exceptions.ResourceError` and then to
    :class:`KeyError` so the resource layer still surfaces a structured
    not-found regardless of the exact FastMCP build in use.
    """
    try:
        from fastmcp.exceptions import NotFoundError

        return NotFoundError(message)
    except ImportError:
        pass
    try:
        from fastmcp.exceptions import ResourceError

        return ResourceError(message)
    except ImportError:
        pass
    return KeyError(message)


def _session_resource(session_id: str) -> str:
    """Return the live JSON for the Mission session ``session_id``.

    Returns a JSON error envelope (``{"error": "session_not_found",
    "session_id": ...}``) when no record exists rather than raising,
    because the synthetic ``read_resource`` tool from the Resources As
    Tools transform expects a string body.
    """
    from mission.state import get_backend

    backend = get_backend()
    session = backend.load_session(session_id)
    if session is None:
        return json.dumps({"error": "session_not_found", "session_id": session_id})
    return json.dumps(_strip_private_fields(session), default=str)


def _session_report_resource(session_id: str) -> str:
    """Return the persisted Final_Report JSON for a terminal session.

    Raises a not-found error (mapped by the FastMCP error-handling
    middleware to MCP code ``-32002``) when the session is missing,
    when it is not yet in a terminal state, or when the matching
    report payload has not been written. The :class:`FilesystemBackend`
    stores reports as sibling ``<session_id>.report.json`` files; other
    backends embed the report under ``session["final_report"]``.
    """
    from mission.state import FilesystemBackend, get_backend
    from mission.types import TERMINAL_STATES

    backend = get_backend()
    session = backend.load_session(session_id)
    if session is None:
        raise _make_not_found(f"Mission session {session_id!r} not found")

    status = session.get("status")
    if status not in TERMINAL_STATES:
        raise _make_not_found(f"Mission session {session_id!r} is not terminal (status={status!r})")

    # ``FilesystemBackend`` persists the report next to the session
    # JSON. Read it back verbatim so the on-disk artifact is the
    # authoritative payload.
    if isinstance(backend, FilesystemBackend):
        report_path = backend.root / f"{session_id}.report.json"
        try:
            return report_path.read_text(encoding="utf-8")
        except FileNotFoundError as err:
            raise _make_not_found(
                f"Mission session {session_id!r} terminal but report not found"
            ) from err

    # Other backends (today, the DynamoDB stub) embed the report on
    # the session itself under ``final_report``.
    report = session.get("final_report")
    if report is None:
        raise _make_not_found(f"Mission session {session_id!r} terminal but report not found")
    return json.dumps(report, default=str)


def _session_audit_replay_resource(session_id: str) -> str:
    """Return the iteration history reconstructed from in-process audit entries.

    Reads from the :class:`mission.audit.MissionAuditCollectorHandler`
    ring buffer attached at server start, projects through
    :func:`mission.audit.replay_audit_entries`, and returns
    ``{"session_id": session_id, "iterations": [...], "note":
    "Reconstructed from in-process audit handler"}`` as JSON.

    Returns ``iterations: []`` when the session is unknown to the
    handler — does NOT 404. The motivation: a Mission session that
    finished before the resource was first read still has a valid
    entry in the persistent session backend, but its audit entries
    may have aged out of the bounded ring buffer. An empty
    reconstruction is the honest answer; a 404 would imply the
    session never existed.
    """
    from mission.audit import get_collector, replay_audit_entries

    collector = get_collector()
    note = "Reconstructed from in-process audit handler"
    if collector is None:
        # No handler attached (e.g. a CLI process that imported the
        # resources package without the install hook firing). Empty
        # reconstruction.
        return json.dumps(
            {"session_id": session_id, "iterations": [], "note": note},
            default=str,
        )

    entries = collector.entries_for(session_id)
    iterations = replay_audit_entries(session_id, entries)
    return json.dumps(
        {"session_id": session_id, "iterations": iterations, "note": note},
        default=str,
    )


def register(mcp_instance: Any) -> None:
    """Register Mission resource templates against the shared MCP server.

    Gated by :data:`feature_flags.FLAG_MISSION`: when the flag is unset
    this function is a no-op so importing this module from
    :mod:`resources` stays side-effect-free. With the flag set, three
    templates are registered and become reachable via the synthetic
    ``read_resource`` tool from the Resources As Tools transform.
    """
    if not is_enabled(FLAG_MISSION):
        return
    # Install the in-process collector when the resource module is
    # registered so the audit-replay path always has a buffer to
    # read from. Idempotent — call-safe across reloads.
    from mission.audit import install_collector

    install_collector()

    mcp_instance.resource("mission://sessions/{session_id}")(_session_resource)
    mcp_instance.resource("mission://sessions/{session_id}/report")(_session_report_resource)
    mcp_instance.resource("mission://sessions/{session_id}/audit-replay")(
        _session_audit_replay_resource
    )
