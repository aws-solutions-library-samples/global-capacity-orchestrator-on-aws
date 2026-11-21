"""The DynamoDB Mission backend keeps a long session under the 400 KB item limit.

A whole session, every iteration's tool results included, is one DynamoDB
item. Past 400 KB ``put_item`` fails, and because the engine saves after an
iteration's tool calls ran, every later ``mission_iterate`` re-ran the same
calls and failed to save again. ``fit_dynamodb_session`` now compacts a copy,
oldest iteration first: tool output first, then whole records down to what
the verdict cascade and history need. A session that cannot fit raises
``SessionTooLargeError`` before anything is written. moto, no real AWS.
"""

from __future__ import annotations

import copy
import json
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import boto3
import pytest
from moto import mock_aws

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "gco_mcp"))

from mission import SCHEMA_VERSION
from mission.state import (
    DYNAMODB_SESSION_BUDGET_BYTES,
    DynamoDBBackend,
    SessionTooLargeError,
    fit_dynamodb_session,
)

#: About 8 KB, the size of a list_jobs page.
_BLOB = {"jobs": [{"name": f"job-{i}", "status": "Running", "pad": "x" * 70} for i in range(100)]}


@pytest.fixture
def backend(monkeypatch: pytest.MonkeyPatch) -> Iterator[DynamoDBBackend]:
    for name, value in {
        "AWS_ACCESS_KEY_ID": "testing",
        "AWS_SECRET_ACCESS_KEY": "testing",
        "AWS_SESSION_TOKEN": "testing",
        "AWS_DEFAULT_REGION": "us-east-2",
    }.items():
        monkeypatch.setenv(name, value)
    with mock_aws():
        boto3.client("dynamodb", region_name="us-east-2").create_table(
            TableName="gco-missions",
            KeySchema=[{"AttributeName": "session_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "session_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        yield DynamoDBBackend(table_name="gco-missions", region="us-east-2")


def _session(**extra: Any) -> dict[str, Any]:
    return {
        "version": SCHEMA_VERSION,
        "session_id": "mission-0002",
        "directive_text": "keep the training job healthy",
        "criteria": [],
        "budget": {"max_iterations": 100, "max_wall_clock_seconds": 36000},
        "tool_allowlist": ["list_jobs"],
        "checkpoint_cadence": {"kind": "every_iteration"},
        "stagnation_threshold": 3,
        "use_sampling": False,
        "allow_scripted_strategies": False,
        "status": "running",
        "created_at": "2026-11-18T00:00:00+00:00",
        "iterations": [],
        "no_progress_counter": 0,
        **extra,
    }


def _iteration(index: int, **extra: Any) -> dict[str, Any]:
    return {
        "iteration_index": index,
        "started_at": "2026-11-18T00:00:00+00:00",
        "ended_at": "2026-11-18T00:00:01+00:00",
        "observation": {"tool_results": [_BLOB, _BLOB], "metrics": {"n": index}, "events": []},
        "criteria_evaluation": [],
        "checkpoint_evaluated": True,
        "verdict": "continue",
        "verdict_reason": "criteria_unmet",
        **extra,
    }


def test_a_long_session_keeps_saving(backend: DynamoDBBackend) -> None:
    """The audit's case: about 20 iterations of two 8 KB results used to fail every save."""
    session = _session()
    for index in range(30):
        session["iterations"].append(_iteration(index))
        backend.save_session(session)  # type: ignore[arg-type]
    loaded = backend.load_session("mission-0002")
    assert loaded is not None
    assert [it["iteration_index"] for it in loaded["iterations"]] == list(range(30))
    newest, oldest = loaded["iterations"][-1], loaded["iterations"][0]
    assert newest["observation"]["tool_results"] == [_BLOB, _BLOB]
    assert "compacted" not in newest
    assert oldest["observation"]["tool_results"] == []
    assert oldest["compacted"] == {"observation.tool_results": 2}
    assert oldest["observation"]["metrics"] == {"n": 0}
    # The engine's in-memory session keeps everything.
    assert session["iterations"][0]["observation"]["tool_results"] == [_BLOB, _BLOB]


def test_a_small_session_is_written_unchanged(backend: DynamoDBBackend) -> None:
    session = _session(iterations=[_iteration(0)])
    backend.save_session(session)  # type: ignore[arg-type]
    assert backend.load_session("mission-0002") == session


def test_records_are_cut_down_once_tool_output_is_gone(backend: DynamoDBBackend) -> None:
    strategy = {"tool_calls": [{"tool_name": "list_jobs", "args": {"note": "y" * 3000}}]}
    session = _session(iterations=[_iteration(i, strategy=strategy) for i in range(120)])
    backend.save_session(session)  # type: ignore[arg-type]
    loaded = backend.load_session("mission-0002")
    assert loaded is not None
    assert len(loaded["iterations"]) == 120
    stub = loaded["iterations"][0]
    assert stub == {
        "iteration_index": 0,
        "started_at": "2026-11-18T00:00:00+00:00",
        "ended_at": "2026-11-18T00:00:01+00:00",
        "verdict": "continue",
        "verdict_reason": "criteria_unmet",
        "checkpoint_evaluated": True,
        "criteria_evaluation": [],
        "compacted": {"record": 1},
    }
    assert loaded["iterations"][-1]["strategy"] == strategy


def test_script_results_and_sampling_text_are_dropped_too() -> None:
    calls = [{"tool_name": "list_jobs", "result_summary": _BLOB}, "not-a-call"]
    session = _session(
        iterations=[
            _iteration(i, script_call_log=copy.deepcopy(calls), sampling_output="z" * 4000)
            for i in range(40)
        ]
        + [{"iteration_index": 40, "verdict": "continue"}]  # no observation at all
    )
    fitted = fit_dynamodb_session(session)
    oldest = fitted["iterations"][0]
    assert oldest["script_call_log"] == [
        {"tool_name": "list_jobs", "result_summary": None},
        "not-a-call",
    ]
    assert oldest["sampling_output"] == ""
    assert oldest["compacted"] == {
        "observation.tool_results": 2,
        "script_call_log.result_summary": 2,
        "sampling_output": 1,
    }
    assert len(json.dumps(fitted).encode()) <= DYNAMODB_SESSION_BUDGET_BYTES


def test_a_session_that_cannot_fit_is_refused_before_writing(backend: DynamoDBBackend) -> None:
    session = _session(directive_text="d" * (DYNAMODB_SESSION_BUDGET_BYTES + 1))
    with pytest.raises(SessionTooLargeError, match="'mission-0002' needs"):
        backend.save_session(session)  # type: ignore[arg-type]
    assert backend.load_session("mission-0002") is None


def test_the_newest_record_is_cut_down_last() -> None:
    """A record with no tool output to drop is cut down to its stub."""
    record = {
        "iteration_index": 0,
        "verdict": "continue",
        "strategy": {"note": "s" * DYNAMODB_SESSION_BUDGET_BYTES},
    }
    fitted = fit_dynamodb_session(_session(iterations=[record]))
    assert fitted["iterations"] == [
        {"iteration_index": 0, "verdict": "continue", "compacted": {"record": 1}}
    ]
