"""Persistence backends for the Mission goal-directed iteration loop.

This module defines the :class:`MissionStateBackend` protocol — the narrow
interface the engine and tool wrappers depend on for loading, saving,
listing, and deleting :class:`~mcp.mission.types.SessionState` records.
Concrete implementations (filesystem, DynamoDB) and the
:func:`get_backend` resolver land in follow-on slices of this file. The
protocol is declared with :func:`typing.runtime_checkable` so tests can
assert backend conformance with ``isinstance`` rather than relying on
duck-typed call sites.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any, Protocol, cast, runtime_checkable

from . import SCHEMA_VERSION
from .types import SessionState

logger = logging.getLogger(__name__)


@runtime_checkable
class MissionStateBackend(Protocol):
    """Storage contract for Mission session records.

    All four methods operate on whole :class:`SessionState` payloads keyed
    by ``session_id``. Implementations are responsible for whatever
    serialization, atomicity, and access-control guarantees their backing
    store provides; callers treat the interface as opaque key-value
    storage with a list operation that returns lightweight metadata
    rather than full session bodies.
    """

    def load_session(self, session_id: str) -> SessionState | None:
        """Return the session record for ``session_id`` or ``None`` if absent.

        Implementations return ``None`` for both unknown ``session_id`` and
        records whose ``version`` does not match the current
        :data:`mcp.mission.SCHEMA_VERSION`; the caller cannot distinguish
        the two and treats both as a missing session.
        """
        ...

    def save_session(self, session: SessionState) -> None:
        """Persist ``session`` keyed by its ``session_id`` field.

        Writes are expected to be atomic from the reader's perspective: a
        concurrent :meth:`load_session` either sees the prior record or
        the new one, never a partial write.
        """
        ...

    def list_sessions(self, filter: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        """Return lightweight metadata for sessions matching ``filter``.

        Each returned dict carries identifying fields (``session_id``,
        ``status``, ``created_at``, and similar) rather than the full
        :class:`SessionState`. ``filter`` is an implementation-defined
        mapping; passing ``None`` lists every session the backend can
        see.
        """
        ...

    def delete_session(self, session_id: str) -> bool:
        """Remove ``session_id`` and return ``True`` if a record was deleted.

        Returns ``False`` when no record existed; implementations do not
        raise on a missing key so that callers can use ``delete_session``
        as an idempotent cleanup primitive.
        """
        ...


class FilesystemBackend:
    """JSON-on-disk implementation of :class:`MissionStateBackend`.

    Each session is persisted as ``<root>/<session_id>.json`` with its
    matching :class:`~mcp.mission.types.SessionState` payload; the
    Final_Report (when present) lives alongside it as
    ``<root>/<session_id>.report.json``. Writes go through the standard
    "temp file in the same directory, ``fsync``, then ``os.replace``"
    pattern so a reader concurrent with a writer always sees either the
    prior version of the file or the new one — never a partial JSON
    document. The temp file lives in the same directory as the final
    target so ``os.replace`` is a same-filesystem rename and therefore
    atomic on POSIX.

    On POSIX systems the root directory is created (and re-asserted) at
    mode ``0o700`` and every session and report file is written at mode
    ``0o600`` so persisted state is unreadable to other local users.
    Permission calls are gated on ``os.name != "nt"`` because the POSIX
    permission model does not apply on Windows; the backend still works
    on Windows, just without the explicit mode tightening.
    """

    def __init__(self, root: Path | None = None) -> None:
        self.root = root if root is not None else Path.home() / ".gco" / "missions"
        self._root_initialized = False

    # ------------------------------------------------------------------ #
    # internals
    # ------------------------------------------------------------------ #

    def _ensure_root(self) -> None:
        """Create the root directory on first use, idempotently.

        We defer the ``mkdir`` to the first write so simply constructing
        a backend (e.g. in the resolver in :func:`get_backend`) does not
        eagerly create ``~/.gco/missions`` on a host that ends up using
        a different backend.
        """
        if self._root_initialized:
            return
        self.root.mkdir(parents=True, exist_ok=True)
        if os.name != "nt":
            with contextlib.suppress(OSError):
                # Best-effort tightening: a directory we already own with
                # different permissions is still safer to use than to
                # refuse the write outright. 0o700 (owner-only) is
                # intentional for ~/.gco/missions: session JSON contains
                # operator-supplied directives, criteria, observations,
                # and tool-call results that should not be readable by
                # other local users.
                # nosemgrep: python.lang.security.audit.insecure-file-permissions.insecure-file-permissions
                os.chmod(self.root, 0o700)
        self._root_initialized = True

    def _session_path(self, session_id: str) -> Path:
        return self.root / f"{session_id}.json"

    def _report_path(self, session_id: str) -> Path:
        return self.root / f"{session_id}.report.json"

    # ------------------------------------------------------------------ #
    # protocol methods
    # ------------------------------------------------------------------ #

    def load_session(self, session_id: str) -> SessionState | None:
        """Return the persisted session or ``None`` for missing/unsupported.

        Returns ``None`` when the file does not exist, when the root
        directory has not been created yet, when the JSON cannot be
        parsed, or when the on-disk ``version`` field does not match
        :data:`mcp.mission.SCHEMA_VERSION`. Version mismatches log a
        single warning naming the unsupported value so an operator can
        spot stale state without having to grep the directory by hand.
        """
        path = self._session_path(session_id)
        try:
            text = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        except OSError:
            return None

        try:
            payload = json.loads(text)
        except ValueError:
            return None

        if not isinstance(payload, dict):
            return None

        version = payload.get("version")
        if version != SCHEMA_VERSION:
            logger.warning(
                "Refusing to load Mission session %s: unsupported schema version %r",
                session_id,
                version,
            )
            return None

        return payload  # type: ignore[return-value]

    def save_session(self, session: SessionState) -> None:
        """Persist ``session`` atomically to ``<root>/<session_id>.json``.

        Opens a temp file in the same directory, dumps JSON, flushes and
        ``fsync``s, applies POSIX mode ``0o600`` (when supported), then
        ``os.replace``s onto the final path. A failure mid-write leaves
        the temp file behind but never replaces the existing final file,
        so the previously-persisted state remains loadable.

        Defense-in-depth strip. The validators in
        :mod:`mcp.mission.validation` attach a cached
        :class:`ast.Expression` under ``_parsed_ast`` on every
        ``predicate`` criterion. That object is not JSON-serialisable;
        a caller that hands a freshly-validated session straight to
        ``save_session`` without first stripping the cache would
        raise :class:`TypeError` at ``json.dump`` time. We strip
        unconditionally here so every persistence path stays correct
        regardless of which caller forgot. The strip is cheap and
        idempotent on already-clean inputs.
        """
        # Local import: ``mission.validation`` is part of the same
        # package so this isn't a cross-package edge, just a
        # dependency-direction kept lazy to keep the eager import
        # surface of ``state`` minimal.
        from .validation import strip_private_fields

        self._ensure_root()
        session_id = session["session_id"]
        final = self._session_path(session_id)
        cleaned = cast("SessionState", strip_private_fields(session))

        try:
            tmp = tempfile.NamedTemporaryFile(  # noqa: SIM115 - explicit close+replace below
                mode="w",
                encoding="utf-8",
                dir=str(self.root),
                prefix=f"{session_id}.",
                suffix=".json.tmp",
                delete=False,
            )
            try:
                json.dump(cleaned, tmp)
                tmp.flush()
                os.fsync(tmp.fileno())
            finally:
                tmp.close()

            if os.name != "nt":
                with contextlib.suppress(OSError):
                    # Same rationale as in ``_ensure_root`` — proceed
                    # with the replace rather than abandoning a write
                    # we already fsynced.
                    os.chmod(tmp.name, 0o600)

            os.replace(tmp.name, final)
        except OSError as exc:
            # Re-raise with the underlying message intact so callers and
            # operators see the real cause (disk full, permission denied,
            # etc.) rather than a wrapped abstraction.
            raise OSError(str(exc)) from exc

    def list_sessions(self, filter: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        """Return summary dicts for every parseable session under ``root``.

        Each entry has the shape ``{"session_id", "status", "created_at",
        "iteration_count"}``. Sessions whose JSON fails to parse, whose
        version is unsupported, or which are missing required summary
        fields are silently skipped (one debug-log line per skip) so a
        single corrupt file cannot block listing the rest.

        ``filter`` currently supports the ``status`` key only; callers
        pass ``{"status": "running"}`` to narrow the list.
        """
        if not self.root.exists():
            return []

        results: list[dict[str, Any]] = []
        for path in self.root.glob("*.json"):
            # Skip the sibling report files — they share the directory
            # but are not session payloads.
            if path.name.endswith(".report.json"):
                continue
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except OSError, ValueError:
                logger.debug("Skipping unreadable Mission file: %s", path)
                continue
            if not isinstance(payload, dict):
                logger.debug("Skipping non-object Mission file: %s", path)
                continue
            if payload.get("version") != SCHEMA_VERSION:
                logger.debug(
                    "Skipping Mission file %s with unknown version %r",
                    path,
                    payload.get("version"),
                )
                continue

            summary = {
                "session_id": payload.get("session_id", path.stem),
                "status": payload.get("status"),
                "created_at": payload.get("created_at"),
                "iteration_count": len(payload.get("iterations", []) or []),
            }
            results.append(summary)

        if filter and "status" in filter:
            wanted = filter["status"]
            results = [r for r in results if r.get("status") == wanted]

        return results

    def delete_session(self, session_id: str) -> bool:
        """Remove the session JSON and any matching report file.

        Returns ``True`` when at least one of the two files existed and
        was removed; ``False`` when neither was present (including when
        the root directory has never been created). The two removals are
        independent so a stale ``.report.json`` left behind by an
        earlier crash is still cleaned up even when the session JSON has
        already been deleted.
        """
        if not self.root.exists():
            return False

        removed = False
        for path in (self._session_path(session_id), self._report_path(session_id)):
            try:
                os.remove(path)
                removed = True
            except FileNotFoundError:
                continue
            except OSError:
                # An unreadable-but-present file should not silently
                # masquerade as "no record existed"; surface it.
                raise
        return removed


class DynamoDBBackend:
    """DynamoDB-backed implementation of :class:`MissionStateBackend`.

    Stub implementation: this class declares the protocol shape so the
    global stack's CDK wiring can reference the backend type and so the
    resolver in slice 3.4 can construct it, but no automated test in
    this slice exercises any code path that touches AWS. Each method
    is annotated with a ``TODO(mission-dynamodb)`` marker right above
    its body and the corresponding tests are skipped (see slice 3.6).
    A separate, AWS-credentialed smoke test validates the real
    behaviour.

    Item schema mirrors the :class:`SessionState` TypedDict one-to-one:
    the partition key is ``session_id`` and ``status`` plus ``created_at``
    feed a ``status-index`` GSI so :meth:`list_sessions` can filter by
    status without a full table scan. ``put_item`` is atomic by virtue
    of DynamoDB's single-item write semantics, so the temp-file dance
    used by :class:`FilesystemBackend` is unnecessary here.

    Table-name resolution is lazy: when the constructor's ``table_name``
    argument is ``None``, the table name is fetched from SSM at
    ``/{project_name}/missions-table-name`` on the first call that
    needs it (not at construction time). This matches the precedent
    pattern in ``cli/models.py`` and lets unit tests construct a
    ``DynamoDBBackend()`` on a host without AWS credentials without
    triggering an SSM call. ``project_name`` is read from the
    ``GCO_PROJECT_NAME`` environment variable, defaulting to ``"gco"``
    so a fresh checkout (or CI run without the env var set) lines up
    with the default project name in ``cli/config.py``.

    The SSM lookup goes through :func:`gco.services.aws_ssm.get_ssm_parameter`,
    the shared helper that consolidates the pattern previously duplicated
    across ``cli/models.py``, ``cli/analytics_user_mgmt.py``, and
    ``gco/services/health_monitor.py``. Putting the helper under
    ``gco/services/`` (rather than ``cli/aws_client.py``) keeps
    ``mcp/`` free of the forbidden ``mcp -> cli`` import edge while
    still letting every backend share one implementation.
    """

    def __init__(self, table_name: str | None = None) -> None:
        self._table_name: str | None = table_name
        self._table: Any = None  # boto3 Table resource, lazily constructed

    # ------------------------------------------------------------------ #
    # internals
    # ------------------------------------------------------------------ #

    def _resolve_table_name(self) -> str:  # pragma: no cover - boto3 / SSM
        """Return the cached table name, fetching from SSM on first call.

        Reads ``GCO_PROJECT_NAME`` (default ``"gco"``) to build the SSM
        parameter path ``/{project_name}/missions-table-name``. The
        value is cached on the instance so subsequent method calls do
        not re-hit SSM.
        """
        if self._table_name is not None:
            return self._table_name

        from gco.services.aws_ssm import get_ssm_parameter

        project_name = os.environ.get("GCO_PROJECT_NAME", "gco")
        param_name = f"/{project_name}/missions-table-name"

        self._table_name = get_ssm_parameter(param_name)
        return self._table_name

    def _get_table(self) -> Any:  # pragma: no cover - boto3 resource
        """Return the cached ``boto3`` Table resource, building it lazily."""
        if self._table is not None:
            return self._table

        import boto3

        self._table = boto3.resource("dynamodb").Table(self._resolve_table_name())
        return self._table

    # ------------------------------------------------------------------ #
    # protocol methods
    # ------------------------------------------------------------------ #

    def load_session(self, session_id: str) -> SessionState | None:  # pragma: no cover - DynamoDB
        """Fetch the session via ``get_item`` keyed on ``session_id``."""
        table = self._get_table()
        response = table.get_item(Key={"session_id": session_id})
        item = response.get("Item")
        if item is None:
            return None
        if item.get("version") != SCHEMA_VERSION:
            logger.warning(
                "Refusing to load Mission session %s: unsupported schema version %r",
                session_id,
                item.get("version"),
            )
            return None
        return cast("SessionState", item)

    def save_session(self, session: SessionState) -> None:  # pragma: no cover - DynamoDB
        """Persist the session via ``put_item`` (atomic single-item write).

        Defense-in-depth strip — same rationale as
        :meth:`FilesystemBackend.save_session`. DynamoDB serialises
        through boto3's own type-converter, which raises
        :class:`TypeError` on an :class:`ast.Expression` just like
        the JSON path; stripping here keeps both backends symmetric.
        """
        from .validation import strip_private_fields

        table = self._get_table()
        table.put_item(Item=strip_private_fields(session))

    def list_sessions(
        self, filter: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:  # pragma: no cover - DynamoDB
        """Return summary dicts via the ``status-index`` GSI.

        When ``filter`` provides a ``status`` key, the call uses the GSI
        partition key directly. With no filter (or any other filter
        shape), this stub falls back to a table ``scan`` so that the
        method still returns the same summary shape as
        :meth:`FilesystemBackend.list_sessions`.
        """
        from boto3.dynamodb.conditions import Key

        table = self._get_table()
        if filter and "status" in filter:
            response = table.query(
                IndexName="status-index",
                KeyConditionExpression=Key("status").eq(filter["status"]),
            )
            items = response.get("Items", [])
        else:
            response = table.scan()
            items = response.get("Items", [])

        return [
            {
                "session_id": item.get("session_id"),
                "status": item.get("status"),
                "created_at": item.get("created_at"),
                "iteration_count": len(item.get("iterations", []) or []),
            }
            for item in items
        ]

    def delete_session(self, session_id: str) -> bool:  # pragma: no cover - DynamoDB
        """Delete the session via ``delete_item`` (idempotent).

        Uses ``ReturnValues="ALL_OLD"`` so the call can distinguish a
        successful deletion from a no-op on a missing key, matching the
        :class:`FilesystemBackend` semantics where the return value
        signals whether anything was actually removed.
        """
        table = self._get_table()
        response = table.delete_item(
            Key={"session_id": session_id},
            ReturnValues="ALL_OLD",
        )
        return response.get("Attributes") is not None


# ---------------------------------------------------------------------- #
# resolver
# ---------------------------------------------------------------------- #

# Recognised values for the ``GCO_MISSION_STATE_BACKEND`` env var. Anything
# outside this set normalises to ``"filesystem"`` — same fallback rule as
# the ``GCO_MCP_TOOL_SEARCH`` precedent in ``mcp/server.py``.
_BACKEND_VALUES = frozenset({"filesystem", "dynamodb"})

# Cached backend instance, populated on first call to ``get_backend()``.
# ``GCO_MISSION_STATE_BACKEND`` is resolved once at first use and the
# resulting instance is reused for every subsequent call. Env vars do not
# change at runtime in practice, and a shared instance keeps the
# ``FilesystemBackend._root_initialized`` cache hot across callers — the
# same module-load resolution pattern used for ``GCO_MCP_TOOL_SEARCH`` in
# ``mcp/server.py``.
_BACKEND_INSTANCE: MissionStateBackend | None = None


def get_backend() -> MissionStateBackend:
    """Return the configured Mission state backend, lazily constructed.

    Reads ``GCO_MISSION_STATE_BACKEND`` on first call. Recognised values
    are ``"filesystem"`` (default) and ``"dynamodb"``; any other value
    logs a single warning naming the unrecognised input and falls back
    to :class:`FilesystemBackend`, matching the unknown-value handling
    for ``GCO_MCP_TOOL_SEARCH`` in ``mcp/server.py``. The resolved
    backend is cached at module scope so subsequent calls return the
    same instance.
    """
    global _BACKEND_INSTANCE
    if _BACKEND_INSTANCE is not None:
        return _BACKEND_INSTANCE

    raw = os.environ.get("GCO_MISSION_STATE_BACKEND", "filesystem").strip().lower()
    if raw == "dynamodb":
        _BACKEND_INSTANCE = DynamoDBBackend()  # pragma: no cover - boto3 path
    elif raw == "filesystem":
        _BACKEND_INSTANCE = FilesystemBackend()
    else:
        logger.warning(
            "Unrecognised GCO_MISSION_STATE_BACKEND value %r; falling back to filesystem",
            raw,
        )
        _BACKEND_INSTANCE = FilesystemBackend()
    return _BACKEND_INSTANCE
