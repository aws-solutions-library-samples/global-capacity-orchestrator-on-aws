"""Round-trip and atomic-write tests for the Mission ``FilesystemBackend``.

The Mission state backend persists every ``SessionState`` as a JSON file
under ``~/.gco/missions/`` (overridable via the constructor's ``root``
argument). Two invariants drive the bulk of these tests:

1. **Round-trip fidelity.** ``save_session`` followed by ``load_session``
   on the same ``session_id`` returns a dict equal to the input — the
   filesystem backend is not allowed to silently rewrite, drop, or
   reorder fields. The schema-version guard rejects on-disk records
   tagged with anything other than the current ``SCHEMA_VERSION``,
   logging a single warning and returning ``None``.

2. **Atomic-write durability.** The save path goes through
   ``tempfile.NamedTemporaryFile`` → ``json.dump`` → ``flush`` →
   ``os.fsync`` → ``os.replace``. A crash mid-write (modeled here by
   patching ``os.fsync`` to raise) must leave the prior on-disk version
   intact: the temp file may be left behind, but the final path is
   never touched. This is the property that lets a Mission session
   survive engine crashes without the operator losing state.

The remaining tests cover POSIX permission tightening, status-filtered
listing, and atomic deletion of the session and its sibling
``.report.json``. A skip-marked placeholder for the DynamoDB backend is
included so ``pytest -v`` shows the deferred coverage explicitly,
matching the task-list directive in slice 3.3.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path
from typing import Any

import pytest

# Mirror the import pattern used by every other ``test_mission_*`` module:
# ``gco_mcp/run_mcp.py`` adds ``gco_mcp/`` to ``sys.path`` at runtime, but tests
# have to do the same before importing ``mission.*``.
sys.path.insert(0, str(Path(__file__).parent.parent / "gco_mcp"))

from mission import SCHEMA_VERSION  # noqa: E402
from mission.state import FilesystemBackend  # noqa: E402

# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def backend(tmp_path: Path) -> FilesystemBackend:
    """A fresh ``FilesystemBackend`` rooted at ``tmp_path``.

    Each test gets an isolated directory so listing and deletion tests
    cannot leak state across cases.
    """
    return FilesystemBackend(root=tmp_path)


def _make_session(session_id: str = "sess-001", status: str = "running") -> dict[str, Any]:
    """Return a minimally-populated ``SessionState`` dict for round-trip tests.

    The dict includes the required keys from
    ``gco_mcp/mission/types.py::SessionState`` plus a sprinkling of nested
    structures (criteria, iterations) so the round-trip assertion
    actually exercises non-trivial JSON paths rather than a flat
    payload.
    """
    return {
        "version": SCHEMA_VERSION,
        "session_id": session_id,
        "directive_text": f"Drive {session_id} to a stable state.",
        "criteria": [
            {
                "criterion_id": "c1",
                "kind": "metric_threshold",
                "required": True,
                "metric": "latency_p95_ms",
                "op": "<",
                "target": 250.0,
            }
        ],
        "budget": {"max_iterations": 10, "max_wall_clock_seconds": 600},
        "tool_allowlist": ["list_jobs", "get_model_uri"],
        "checkpoint_cadence": {"kind": "every_iteration"},
        "stagnation_threshold": 3,
        "use_sampling": False,
        "allow_scripted_strategies": False,
        "status": status,
        "created_at": "2025-01-01T00:00:00Z",
        "iterations": [],
        "no_progress_counter": 0,
    }


# ---------------------------------------------------------------------------
# Round-trip
# ---------------------------------------------------------------------------


def test_save_load_round_trip(backend: FilesystemBackend) -> None:
    """``save_session`` followed by ``load_session`` returns the exact dict."""
    session = _make_session()
    backend.save_session(session)

    loaded = backend.load_session("sess-001")

    assert loaded == session


# ---------------------------------------------------------------------------
# Atomic write durability
# ---------------------------------------------------------------------------


def test_atomic_write_survives_mid_write_crash(
    backend: FilesystemBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Patching ``os.fsync`` to raise leaves the prior on-disk version intact.

    Sequence:

    1. The first ``save_session`` lands a known-good payload on disk.
    2. ``os.fsync`` is then patched to raise ``OSError`` so the second
       ``save_session`` aborts mid-write — after ``json.dump`` returns
       but before ``os.replace`` runs.
    3. The original ``session_id`` is reloaded; the assertion checks the
       original payload is still the one on disk.

    The patched ``fsync`` must raise in a way that lets the temp file's
    cleanup ``finally`` block run, otherwise we'd be testing a leaked
    file handle rather than the atomic-write invariant. A plain
    ``OSError`` raised inside the inner ``try`` does the right thing:
    the outer ``finally`` runs ``tmp.close()`` before the exception
    propagates.
    """
    original = _make_session(session_id="sess-atomic", status="running")
    backend.save_session(original)
    assert backend.load_session("sess-atomic") == original

    def _raise_oserror(*args: Any, **kwargs: Any) -> None:
        raise OSError("simulated mid-write fsync failure")

    monkeypatch.setattr(os, "fsync", _raise_oserror)

    # The would-be "next" version that should never be written.
    next_version = _make_session(session_id="sess-atomic", status="completed")
    next_version["no_progress_counter"] = 99

    with pytest.raises(OSError, match="simulated mid-write fsync failure"):
        backend.save_session(next_version)

    # Drop the patch before reload so ``load_session`` runs unaffected.
    monkeypatch.undo()

    reloaded = backend.load_session("sess-atomic")
    assert reloaded == original
    assert reloaded != next_version


# ---------------------------------------------------------------------------
# Schema version guard
# ---------------------------------------------------------------------------


def test_load_rejects_unknown_schema_version(
    backend: FilesystemBackend,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A file with ``version: 999`` returns ``None`` and emits a warning.

    The session is written with raw ``json.dumps`` rather than through
    ``save_session`` so we actually exercise the loader's version check
    (the writer would correctly stamp ``SCHEMA_VERSION``).
    """
    # Force the root directory to exist so the loader actually runs the
    # read path; ``FilesystemBackend`` defers ``mkdir`` to first save.
    tmp_path.mkdir(parents=True, exist_ok=True)
    bad_payload = _make_session(session_id="sess-future")
    bad_payload["version"] = 999

    (tmp_path / "sess-future.json").write_text(json.dumps(bad_payload), encoding="utf-8")

    with caplog.at_level(logging.WARNING, logger="mission.state"):
        result = backend.load_session("sess-future")

    assert result is None
    assert any(
        "unsupported schema version" in record.getMessage() and "sess-future" in record.getMessage()
        for record in caplog.records
    ), f"expected warning about unsupported schema version, got: {caplog.text!r}"


# ---------------------------------------------------------------------------
# POSIX permissions
# ---------------------------------------------------------------------------


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission model only")
def test_posix_permissions_set_on_save(backend: FilesystemBackend, tmp_path: Path) -> None:
    """File mode ``0o600`` and directory mode ``0o700`` after the first save."""
    backend.save_session(_make_session(session_id="sess-perms"))

    session_path = tmp_path / "sess-perms.json"
    assert session_path.exists()

    file_mode = os.stat(session_path).st_mode & 0o777
    dir_mode = os.stat(tmp_path).st_mode & 0o777

    assert file_mode == 0o600, f"expected file mode 0o600, got {oct(file_mode)}"
    assert dir_mode == 0o700, f"expected directory mode 0o700, got {oct(dir_mode)}"


# ---------------------------------------------------------------------------
# Listing with status filter
# ---------------------------------------------------------------------------


def test_list_sessions_returns_summaries(backend: FilesystemBackend) -> None:
    """``list_sessions(filter={"status": "running"})`` returns only the running ones.

    Three sessions with mixed statuses (``running``, ``completed``,
    ``running``) are saved; the filter must select exactly the two
    ``running`` records and the summary must carry the documented keys.
    """
    backend.save_session(_make_session(session_id="r1", status="running"))
    backend.save_session(_make_session(session_id="c1", status="completed"))
    backend.save_session(_make_session(session_id="r2", status="running"))

    running = backend.list_sessions(filter={"status": "running"})

    ids = sorted(entry["session_id"] for entry in running)
    assert ids == ["r1", "r2"]
    for entry in running:
        assert entry["status"] == "running"
        assert "created_at" in entry
        assert "iteration_count" in entry


# ---------------------------------------------------------------------------
# Deletion (session + sibling report)
# ---------------------------------------------------------------------------


def test_delete_session_removes_session_and_report(
    backend: FilesystemBackend, tmp_path: Path
) -> None:
    """``delete_session`` removes both the session JSON and ``.report.json``."""
    session = _make_session(session_id="sess-done", status="completed")
    backend.save_session(session)

    session_path = tmp_path / "sess-done.json"
    report_path = tmp_path / "sess-done.report.json"
    report_path.write_text(json.dumps({"session_id": "sess-done", "verdict": "complete"}))

    assert session_path.exists()
    assert report_path.exists()

    deleted = backend.delete_session("sess-done")

    assert deleted is True
    assert not session_path.exists()
    assert not report_path.exists()


# ---------------------------------------------------------------------------
# DynamoDB backend placeholder
# ---------------------------------------------------------------------------


@pytest.mark.skip(reason="DynamoDB backend smoke-tested separately")
def test_dynamodb_backend_smoke() -> None:
    """Placeholder so the deferred DynamoDB coverage is visible in pytest output.

    The real ``DynamoDBBackend`` is exercised by an AWS-credentialed
    smoke test outside this PR's scope. The class still has to import
    cleanly here so the global stack's CDK wiring can reference its
    type, which is verified by simply importing the module at the top
    of this file.
    """


# ---------------------------------------------------------------------------
# Defense-in-depth: persistence layer strips _parsed_ast
# ---------------------------------------------------------------------------


def test_save_session_strips_parsed_ast_defensively(backend: FilesystemBackend) -> None:
    """``save_session`` must succeed on a session that still carries cached AST.

    Pin the persistence-layer's defense-in-depth strip. The validators
    in ``gco_mcp/mission/validation.py`` attach a cached
    :class:`ast.Expression` under ``_parsed_ast`` on every
    ``predicate`` criterion. Earlier slices required every caller to
    strip those keys before hand-off; a single missed callsite caused
    a real production bug where ``mission_start`` raised
    ``TypeError('Object of type Expression is not JSON serializable')``
    on every predicate criterion.

    The fix moved the strip to ``FilesystemBackend.save_session`` so
    every callsite is correct by construction. This test pins that
    contract: a session whose criteria still carry ``_parsed_ast``
    must round-trip without any caller-side intervention.
    """
    from mission.predicate import parse_predicate

    session = _make_session(session_id="sess-with-cached-ast")
    # Replace the metric_threshold criterion with a predicate carrying
    # the parser-attached ast.Expression cache. ``parse_predicate``
    # returns the same shape ``validate_criteria`` would attach.
    parsed_ast = parse_predicate("len(obs['tool_results']) > 0")
    session["criteria"] = [
        {
            "criterion_id": "predicate_with_cached_ast",
            "kind": "predicate",
            "required": True,
            "expression": "len(obs['tool_results']) > 0",
            "_parsed_ast": parsed_ast,
        }
    ]

    # The save would have raised TypeError pre-fix because
    # json.dump can't serialize ast.Expression. Post-fix the strip
    # at save_session time keeps the call correct.
    backend.save_session(session)  # type: ignore[arg-type]

    # Round-trip — the loaded session has the criteria minus the
    # private cache. The expression string is preserved so the engine
    # re-parses on demand.
    loaded = backend.load_session("sess-with-cached-ast")
    assert loaded is not None
    crits = loaded["criteria"]
    assert len(crits) == 1
    crit = crits[0]
    assert crit["criterion_id"] == "predicate_with_cached_ast"
    assert crit["expression"] == "len(obs['tool_results']) > 0"
    # The cached AST is gone — that's the whole point.
    assert "_parsed_ast" not in crit


def test_save_session_strips_parsed_ast_from_iteration_history(
    backend: FilesystemBackend,
) -> None:
    """Iteration history's ``criteria_evaluation`` shape is also stripped.

    When the engine writes an iteration record after an Evaluate_Phase
    that ran predicate criteria, the ``criteria_evaluation`` entries
    can carry the same ``_parsed_ast`` cache. The strip walks both
    ``criteria`` and ``iterations`` so the on-disk JSON is fully
    portable regardless of which path produced the in-memory shape.
    """
    from mission.predicate import parse_predicate

    parsed_ast = parse_predicate("True")
    session = _make_session(session_id="sess-iter-history-strip")
    session["iterations"] = [
        {
            "iteration_index": 0,
            "started_at": "2025-01-01T00:00:00Z",
            "ended_at": "2025-01-01T00:00:01Z",
            "phases": [],
            "strategy": {},
            "observation": {},
            "criteria_evaluation": [
                {
                    "criterion_id": "p1",
                    "status": "met",
                    "evidence": True,
                    "_parsed_ast": parsed_ast,
                }
            ],
            "verdict": "complete",
            "verdict_reason": "criteria_met",
            "checkpoint_evaluated": True,
        }
    ]

    backend.save_session(session)  # type: ignore[arg-type]

    loaded = backend.load_session("sess-iter-history-strip")
    assert loaded is not None
    iterations = loaded.get("iterations") or []
    assert len(iterations) == 1
    eval_entries = iterations[0].get("criteria_evaluation") or []
    assert len(eval_entries) == 1
    assert eval_entries[0]["criterion_id"] == "p1"
    assert eval_entries[0]["status"] == "met"
    # The cached AST in the iteration evaluation is gone too.
    assert "_parsed_ast" not in eval_entries[0]
