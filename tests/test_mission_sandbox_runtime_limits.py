"""Mission script sandbox runtime limits, run against the real Monty provider.

* The script cap (``GCO_MCP_CODE_MODE_MAX_DURATION_SECS``) is a wall-clock
  limit that includes time spent waiting on tool calls. Monty's own feed-time
  limit counts only time inside the VM, so a script looping over tool calls
  used to run for as long as it liked.
* A tool call still in flight when the cap fires is recorded as failed in the
  partial script-call log and in the audit stream.
* Errors raised inside the script still end the run through Monty's error
  path.
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "gco_mcp"))

from mission import sandbox as sandbox_module


def _session(iterations: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return {
        "session_id": "sess-limits",
        "directive_text": "goal",
        "criteria": [],
        "budget": {},
        "iterations": list(iterations or []),
    }


@pytest.fixture
def audit_rows(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Capture the ``mission_script_call_event`` rows the tool wrappers emit."""
    rows: list[dict[str, Any]] = []

    def record(
        session_id: str,
        iteration_index: int,
        tool_name: str,
        status: str,
        duration_ms: int,
        error_message: str | None = None,
    ) -> None:
        rows.append(
            {
                "session_id": session_id,
                "iteration_index": iteration_index,
                "tool_name": tool_name,
                "status": status,
                "error_message": error_message,
            }
        )

    monkeypatch.setattr(sandbox_module._audit, "emit_script_call_event", record)
    return rows


class TestWallClockCap:
    async def test_cap_counts_time_spent_waiting_on_tool_calls(
        self, monkeypatch: pytest.MonkeyPatch, audit_rows: list[dict[str, Any]]
    ) -> None:
        """300 calls of 20 ms each would take 6 s; a 0.5 s cap stops the script."""
        monkeypatch.setattr(sandbox_module, "_DURATION_LIMIT_SECS", 0.5)
        calls = 0

        async def dispatcher(name: str, args: dict[str, Any], ctx: Any) -> dict[str, Any]:
            nonlocal calls
            calls += 1
            await asyncio.sleep(0.02)
            return {"ok": True}

        box = sandbox_module.MissionSandbox(["list_jobs"], _session())
        script = "i = 0\nwhile i < 300:\n    r = await list_jobs()\n    i = i + 1\n"

        started = time.monotonic()
        with pytest.raises(sandbox_module.SandboxTerminated) as exc_info:
            await box.run(script, None, dispatcher)
        elapsed = time.monotonic() - started

        assert elapsed < 4.0, f"script ran {elapsed:.2f}s past a 0.5s cap"
        assert exc_info.value.cause == "TimeoutError"
        partial = exc_info.value.partial_script_call_log
        assert 0 < len(partial) < 300
        assert calls < 300
        assert len(audit_rows) == len(partial)

    async def test_a_call_in_flight_when_the_cap_fires_is_recorded(
        self, monkeypatch: pytest.MonkeyPatch, audit_rows: list[dict[str, Any]]
    ) -> None:
        monkeypatch.setattr(sandbox_module, "_DURATION_LIMIT_SECS", 0.3)

        async def dispatcher(name: str, args: dict[str, Any], ctx: Any) -> dict[str, Any]:
            await asyncio.sleep(30)
            return {"ok": True}

        box = sandbox_module.MissionSandbox(["list_jobs"], _session())

        with pytest.raises(sandbox_module.SandboxTerminated) as exc_info:
            await box.run("r = await list_jobs(region='us-east-1')\n", None, dispatcher)

        [entry] = exc_info.value.partial_script_call_log
        assert entry["tool_name"] == "list_jobs"
        assert entry["args"] == {"region": "us-east-1"}
        assert entry["status"] == "failed"
        assert entry["error_message"].startswith("CancelledError")
        assert [(row["tool_name"], row["status"]) for row in audit_rows] == [
            ("list_jobs", "failed")
        ]

    async def test_a_script_that_finishes_inside_the_cap_returns_its_observation(
        self, monkeypatch: pytest.MonkeyPatch, audit_rows: list[dict[str, Any]]
    ) -> None:
        monkeypatch.setattr(sandbox_module, "_DURATION_LIMIT_SECS", 5.0)

        async def dispatcher(name: str, args: dict[str, Any], ctx: Any) -> dict[str, Any]:
            return {"metrics": {"loss": 0.1}}

        box = sandbox_module.MissionSandbox(["list_jobs"], _session())

        observation, calls = await box.run("r = await list_jobs()\n", None, dispatcher)

        assert observation["metrics"] == {"loss": 0.1}
        assert [call["status"] for call in calls] == ["ok"]

    async def test_a_script_error_still_terminates_through_the_monty_error_path(
        self, monkeypatch: pytest.MonkeyPatch, audit_rows: list[dict[str, Any]]
    ) -> None:
        monkeypatch.setattr(sandbox_module, "_DURATION_LIMIT_SECS", 30.0)

        async def dispatcher(name: str, args: dict[str, Any], ctx: Any) -> dict[str, Any]:
            return {"ok": True}

        box = sandbox_module.MissionSandbox(["list_jobs"], _session())

        with pytest.raises(sandbox_module.SandboxTerminated) as exc_info:
            await box.run("r = await list_jobs()\nd = {}\nx = d['missing']\n", None, dispatcher)

        assert exc_info.value.cause == "MontyRuntimeError"
        assert [call["status"] for call in exc_info.value.partial_script_call_log] == ["ok"]

    async def test_a_timeout_error_the_cap_did_not_raise_propagates(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Only an expired cap becomes SandboxTerminated; any other TimeoutError
        out of the provider is a sandbox failure the engine must see."""

        class TimingOutProvider:
            def __init__(self, *, limits: dict[str, Any]) -> None:
                self.limits = limits

            async def run(self, **_kwargs: Any) -> None:
                raise TimeoutError("provider transport timed out")

        class _NeverRaised(Exception):
            pass

        monkeypatch.setattr(
            sandbox_module, "_import_provider", lambda: (TimingOutProvider, _NeverRaised)
        )
        monkeypatch.setattr(sandbox_module, "_DURATION_LIMIT_SECS", 30.0)
        box = sandbox_module.MissionSandbox(["list_jobs"], _session())

        async def dispatcher(name: str, args: dict[str, Any], ctx: Any) -> None:
            return None

        with pytest.raises(TimeoutError, match="provider transport timed out"):
            await box.run("r = await list_jobs()\n", None, dispatcher)
