"""CLI tests for the ``gco mission memory`` subcommand group.

Drives ``search`` / ``list`` / ``backfill`` through ``CliRunner`` with the
``MissionMemoryStore`` class patched at its import site
(``mission.memory.MissionMemoryStore`` — every command constructs the
store through a local import, so the patch swaps in a stub at call
time). No SSM, Bedrock, or DynamoDB is ever reached.

Pinned behaviours: the ``GCO_ENABLE_MISSION`` gate (exit 2 with the
feature hint), argument forwarding, JSON and table output shapes, the
deployment hint + structured envelope + exit 1 when the memory
infrastructure is unavailable, and the backfill contract — terminal
reports written, non-terminal or malformed files counted as skipped or
failed without aborting the run, per-report failures isolated and
flipping the exit code, and infrastructure absence stopping
immediately (nothing later can succeed either).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from click.testing import CliRunner

# Mirror the path-injection pattern used by the other test_mission_* files.
sys.path.insert(0, str(Path(__file__).parent.parent / "gco_mcp"))
from mission.memory import (  # noqa: E402
    MissionMemoryError,
    MissionMemoryUnavailableError,
)

from cli.main import cli  # noqa: E402

# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------


class _StubStore:
    """Duck-typed MissionMemoryStore; class-level recording across instances."""

    search_calls: list[tuple[str, int, str | None]] = []
    list_calls: list[int] = []
    write_calls: list[dict[str, Any]] = []
    search_results: list[dict[str, Any]] = []
    list_results: list[dict[str, Any]] = []
    raise_on_search: Exception | None = None
    raise_on_list: Exception | None = None
    raise_on_write: Exception | None = None
    raise_on_write_for: str | None = None

    @classmethod
    def reset(cls) -> None:
        cls.search_calls = []
        cls.list_calls = []
        cls.write_calls = []
        cls.search_results = []
        cls.list_results = []
        cls.raise_on_search = None
        cls.raise_on_list = None
        cls.raise_on_write = None
        cls.raise_on_write_for = None

    def search_similar(
        self, directive: str, top_k: int = 3, final_verdict: str | None = None
    ) -> list[dict[str, Any]]:
        type(self).search_calls.append((directive, top_k, final_verdict))
        if type(self).raise_on_search is not None:
            raise type(self).raise_on_search
        return list(type(self).search_results)

    def list_memories(self, limit: int = 50) -> list[dict[str, Any]]:
        type(self).list_calls.append(limit)
        if type(self).raise_on_list is not None:
            raise type(self).raise_on_list
        return list(type(self).list_results)

    def write_memory(
        self,
        session: dict[str, Any],
        verdict: str,
        reason: str,
        lessons: str,
        followups: list[str],
    ) -> None:
        if type(self).raise_on_write is not None and (
            type(self).raise_on_write_for is None
            or session.get("session_id") == type(self).raise_on_write_for
        ):
            raise type(self).raise_on_write
        type(self).write_calls.append(
            {
                "session_id": session.get("session_id"),
                "verdict": verdict,
                "reason": reason,
                "lessons": lessons,
                "followups": followups,
            }
        )


@pytest.fixture(autouse=True)
def _stub_store(monkeypatch: pytest.MonkeyPatch):
    """Enable the feature flag and patch the store class for every test."""
    monkeypatch.setenv("GCO_ENABLE_MISSION", "true")
    _StubStore.reset()
    with patch("mission.memory.MissionMemoryStore", _StubStore):
        yield


def _invoke(*args: str) -> Any:
    return CliRunner().invoke(cli, ["mission", "memory", *args])


def _report(session_id: str, *, verdict: str = "complete") -> dict[str, Any]:
    return {
        "session_id": session_id,
        "directive_text": f"directive for {session_id}",
        "criteria": [{"criterion_id": "c1", "kind": "metric_threshold"}],
        "tool_allowlist": ["find_examples"],
        "created_at": "2026-08-01T00:00:00+00:00",
        "ended_at": "2026-08-01T01:00:00+00:00",
        "iterations": [{}],
        "final_verdict": verdict,
        "final_verdict_reason": "criteria_met",
        "lessons": f"lessons for {session_id}",
        "recommended_followups": ["follow up"],
    }


# ---------------------------------------------------------------------------
# Feature-flag gate
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "argv",
    [("search", "anything"), ("list",), ("backfill",)],
    ids=["search", "list", "backfill"],
)
def test_flag_gate_blocks_every_memory_command(
    monkeypatch: pytest.MonkeyPatch, argv: tuple[str, ...]
) -> None:
    monkeypatch.delenv("GCO_ENABLE_MISSION", raising=False)
    monkeypatch.delenv("GCO_ENABLE_ALL_TOOLS", raising=False)
    result = _invoke(*argv)
    assert result.exit_code == 2
    assert "GCO_ENABLE_MISSION" in result.output


# ---------------------------------------------------------------------------
# search
# ---------------------------------------------------------------------------


class TestSearch:
    def test_json_output_and_argument_forwarding(self) -> None:
        _StubStore.search_results = [
            {
                "session_id": "sess-prior-001",
                "directive": "old directive",
                "lessons": "what worked",
                "final_verdict": "complete",
                "score": 0.91,
            }
        ]
        result = _invoke("search", "reduce loss", "--top-k", "5", "--verdict", "complete")
        assert result.exit_code == 0, result.output
        assert json.loads(result.output) == {"results": _StubStore.search_results}
        assert _StubStore.search_calls == [("reduce loss", 5, "complete")]

    def test_defaults(self) -> None:
        result = _invoke("search", "reduce loss")
        assert result.exit_code == 0, result.output
        assert _StubStore.search_calls == [("reduce loss", 3, None)]

    def test_table_output(self) -> None:
        _StubStore.search_results = [
            {
                "session_id": "sess-prior-001",
                "directive": "old directive",
                "final_verdict": "complete",
                "score": 0.91,
            }
        ]
        result = _invoke("search", "reduce loss", "--output", "table")
        assert result.exit_code == 0, result.output
        assert "SCORE" in result.output
        assert "sess-prior-001" in result.output
        assert "0.910" in result.output

    def test_unavailable_prints_hint_and_exits_1(self) -> None:
        _StubStore.raise_on_search = MissionMemoryUnavailableError("table not found")
        result = _invoke("search", "anything")
        assert result.exit_code == 1
        assert "mission_memory.enabled" in result.output
        assert "mission_memory_unavailable" in result.output

    def test_hard_error_envelopes_and_exits_1(self) -> None:
        _StubStore.raise_on_search = MissionMemoryError("embedding exploded")
        result = _invoke("search", "anything")
        assert result.exit_code == 1
        assert "mission_memory_search_failed" in result.output
        assert "embedding exploded" in result.output


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------


class TestList:
    def test_json_output_and_limit_forwarding(self) -> None:
        _StubStore.list_results = [
            {
                "session_id": "sess-1",
                "directive": "old directive",
                "final_verdict": "complete",
                "iteration_count": 4,
                "completed_at": "2026-08-01T01:00:00+00:00",
            }
        ]
        result = _invoke("list", "--limit", "7")
        assert result.exit_code == 0, result.output
        assert json.loads(result.output) == {"memories": _StubStore.list_results}
        assert _StubStore.list_calls == [7]

    def test_table_output(self) -> None:
        _StubStore.list_results = [
            {
                "session_id": "sess-1",
                "directive": "old directive",
                "final_verdict": "complete",
                "iteration_count": 4,
                "completed_at": "2026-08-01T01:00:00+00:00",
            }
        ]
        result = _invoke("list", "--output", "table")
        assert result.exit_code == 0, result.output
        assert "COMPLETED" in result.output
        assert "sess-1" in result.output

    def test_unavailable_prints_hint_and_exits_1(self) -> None:
        _StubStore.raise_on_list = MissionMemoryUnavailableError("table not found")
        result = _invoke("list")
        assert result.exit_code == 1
        assert "mission_memory_unavailable" in result.output


# ---------------------------------------------------------------------------
# backfill
# ---------------------------------------------------------------------------


class TestBackfill:
    def test_writes_terminal_reports_and_skips_the_rest(self, tmp_path: Path) -> None:
        (tmp_path / "a.report.json").write_text(json.dumps(_report("sess-a")))
        (tmp_path / "b.report.json").write_text(json.dumps(_report("sess-b", verdict="terminate")))
        # Non-terminal shape: counted as skipped, not failed.
        (tmp_path / "c.report.json").write_text(json.dumps({"session_id": "sess-c"}))
        # Unreadable JSON: counted as failed (isolated, run continues).
        (tmp_path / "d.report.json").write_text("{not json")

        result = _invoke("backfill", "--root", str(tmp_path))

        assert result.exit_code == 1  # the unreadable file flips the exit code
        payload = json.loads(result.output.strip().splitlines()[-1])
        assert payload["written"] == 2
        assert payload["skipped"] == 1
        assert payload["failed"] == 1
        assert payload["failures"][0]["file"] == "d.report.json"
        written = {call["session_id"]: call for call in _StubStore.write_calls}
        assert set(written) == {"sess-a", "sess-b"}
        assert written["sess-a"]["verdict"] == "complete"
        assert written["sess-a"]["lessons"] == "lessons for sess-a"
        assert written["sess-a"]["followups"] == ["follow up"]
        assert written["sess-b"]["verdict"] == "terminate"

    def test_clean_run_exits_zero(self, tmp_path: Path) -> None:
        (tmp_path / "a.report.json").write_text(json.dumps(_report("sess-a")))
        result = _invoke("backfill", "--root", str(tmp_path))
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output.strip().splitlines()[-1])
        assert payload == {
            "written": 1,
            "skipped": 0,
            "failed": 0,
            "root": str(tmp_path),
        }

    def test_empty_root_is_a_clean_noop(self, tmp_path: Path) -> None:
        result = _invoke("backfill", "--root", str(tmp_path))
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output.strip().splitlines()[-1])
        assert payload["written"] == 0

    def test_per_report_failure_is_isolated(self, tmp_path: Path) -> None:
        (tmp_path / "a.report.json").write_text(json.dumps(_report("sess-a")))
        (tmp_path / "b.report.json").write_text(json.dumps(_report("sess-b")))
        _StubStore.raise_on_write = MissionMemoryError("throttled")
        _StubStore.raise_on_write_for = "sess-a"

        result = _invoke("backfill", "--root", str(tmp_path))

        assert result.exit_code == 1
        payload = json.loads(result.output.strip().splitlines()[-1])
        assert payload["written"] == 1  # sess-b still landed
        assert payload["failed"] == 1
        assert payload["failures"][0]["file"] == "a.report.json"
        assert [c["session_id"] for c in _StubStore.write_calls] == ["sess-b"]

    def test_unavailable_stops_immediately_with_hint(self, tmp_path: Path) -> None:
        (tmp_path / "a.report.json").write_text(json.dumps(_report("sess-a")))
        (tmp_path / "b.report.json").write_text(json.dumps(_report("sess-b")))
        _StubStore.raise_on_write = MissionMemoryUnavailableError("table not found")

        result = _invoke("backfill", "--root", str(tmp_path))

        assert result.exit_code == 1
        assert "mission_memory_unavailable" in result.output
        assert _StubStore.write_calls == []  # nothing landed, nothing retried
