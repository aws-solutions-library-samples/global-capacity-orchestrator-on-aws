"""Floci layer: the Mission ``DynamoDBBackend`` over a production-shaped table.

``gco_mcp/mission/state.py`` ships two session backends. The filesystem one
is unit-tested in depth; the DynamoDB one was excluded from coverage with a
note that a credentialed smoke test would validate it — and no such test
existed. Here it runs unmodified against the emulator, over a table shaped
exactly like ``gco/stacks/global_stack.py`` provisions (``session_id``
partition key, ``status-index`` GSI on ``status`` + ``created_at``) and with
the ``/<project>/missions-table-name`` parameter the stack publishes for the
lazy table-name resolution.

The first run of this module found the backend could not save a real session
at all: boto3's resource API rejects ``float``, and every session carries
floats in criterion targets and observed metrics. The conversions that fix it
are pinned in ``tests/test_mission_state.py``; this module proves the
resulting round trip on the wire, including the GSI-backed status listing.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import boto3
import pytest

from tests._floci import floci_test_markers, unique_name

pytestmark = floci_test_markers()

# ``gco_mcp/run_mcp.py`` adds ``gco_mcp/`` to ``sys.path`` at runtime; the
# mission tests import ``mission.*`` the same way.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "gco_mcp"))


def _session(session_id: str, *, status: str = "running", created_at: str) -> dict:
    """A session the way ``mission_start`` would persist it: floats included."""
    from mission import SCHEMA_VERSION

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
                "target": 250.5,
            }
        ],
        "budget": {"max_iterations": 10, "max_wall_clock_seconds": 600},
        "tool_allowlist": ["list_jobs", "get_model_uri"],
        "checkpoint_cadence": {"kind": "every_iteration"},
        "stagnation_threshold": 3,
        "use_sampling": False,
        "allow_scripted_strategies": False,
        "status": status,
        "created_at": created_at,
        "iterations": [
            {
                "iteration": 1,
                "observed": {"latency_p95_ms": 312.25, "healthy": True, "errors": 0},
            }
        ],
        "no_progress_counter": 0,
    }


@pytest.fixture(scope="module")
def missions_table(verified_floci_endpoint):
    """The missions table as the global stack declares it, plus its SSM name."""
    project = unique_name("gcomission").replace("-", "")[:16]
    table_name = f"{project}-missions"
    dynamodb = boto3.client("dynamodb")
    dynamodb.create_table(
        TableName=table_name,
        AttributeDefinitions=[
            {"AttributeName": "session_id", "AttributeType": "S"},
            {"AttributeName": "status", "AttributeType": "S"},
            {"AttributeName": "created_at", "AttributeType": "S"},
        ],
        KeySchema=[{"AttributeName": "session_id", "KeyType": "HASH"}],
        GlobalSecondaryIndexes=[
            {
                "IndexName": "status-index",
                "KeySchema": [
                    {"AttributeName": "status", "KeyType": "HASH"},
                    {"AttributeName": "created_at", "KeyType": "RANGE"},
                ],
                "Projection": {"ProjectionType": "ALL"},
            }
        ],
        BillingMode="PAY_PER_REQUEST",
    )
    dynamodb.get_waiter("table_exists").wait(TableName=table_name)
    ssm = boto3.client("ssm")
    ssm.put_parameter(
        Name=f"/{project}/missions-table-name", Value=table_name, Type="String", Overwrite=True
    )
    yield {"project": project, "table_name": table_name}
    ssm.delete_parameter(Name=f"/{project}/missions-table-name")
    dynamodb.delete_table(TableName=table_name)


@pytest.fixture()
def backend(missions_table):
    from mission.state import DynamoDBBackend

    return DynamoDBBackend(table_name=missions_table["table_name"])


def test_the_global_stack_declares_the_shape_this_module_provisions():
    """Guard the fixture against drifting from the CDK table definition."""
    source = (
        Path(__file__).resolve().parents[1] / "gco" / "stacks" / "global_stack.py"
    ).read_text()
    missions = source[source.index('"MissionsTable"') :]
    assert 'table_name=f"{project_name}-missions"' in missions
    assert 'name="session_id"' in missions
    assert 'index_name="status-index"' in missions
    assert 'parameter_name=f"/{project_name}/missions-table-name"' in missions


class TestSessionRoundTrip:
    def test_a_session_with_floats_saves_and_loads_as_plain_python(self, backend):
        session = _session(unique_name("sess"), created_at="2026-09-12T10:00:00Z")

        backend.save_session(session)
        loaded = backend.load_session(session["session_id"])

        assert loaded == session
        assert type(loaded["criteria"][0]["target"]) is float
        assert type(loaded["iterations"][0]["observed"]["latency_p95_ms"]) is float
        assert type(loaded["version"]) is int
        assert loaded["iterations"][0]["observed"]["healthy"] is True
        json.dumps(loaded)  # no Decimal may leak out of the backend

    def test_a_missing_session_is_none(self, backend):
        assert backend.load_session(unique_name("never-saved")) is None

    def test_an_unsupported_schema_version_is_refused(self, backend, missions_table, caplog):
        from mission import SCHEMA_VERSION

        session_id = unique_name("sess-old")
        boto3.resource("dynamodb").Table(missions_table["table_name"]).put_item(
            Item={"session_id": session_id, "version": SCHEMA_VERSION + 1, "status": "running"}
        )
        with caplog.at_level("WARNING"):
            assert backend.load_session(session_id) is None
        assert "unsupported schema version" in caplog.text

    def test_delete_reports_whether_anything_was_removed(self, backend):
        session = _session(unique_name("sess-del"), created_at="2026-09-12T10:05:00Z")
        backend.save_session(session)

        assert backend.delete_session(session["session_id"]) is True
        assert backend.load_session(session["session_id"]) is None
        assert backend.delete_session(session["session_id"]) is False


class TestStatusListing:
    def test_the_status_index_serves_filtered_listings(self, backend):
        marker = unique_name("run")
        running = [
            _session(
                f"{marker}-r{index}", status="running", created_at=f"2026-09-12T1{index}:00:00Z"
            )
            for index in range(3)
        ]
        completed = _session(
            f"{marker}-done", status="completed", created_at="2026-09-12T09:00:00Z"
        )
        for session in [*running, completed]:
            backend.save_session(session)

        listed = [
            item
            for item in backend.list_sessions({"status": "running"})
            if str(item["session_id"]).startswith(marker)
        ]
        assert sorted(item["session_id"] for item in listed) == sorted(
            session["session_id"] for session in running
        )
        assert {item["status"] for item in listed} == {"running"}
        assert all(item["iteration_count"] == 1 for item in listed)
        assert all(type(item["iteration_count"]) is int for item in listed)

        everything = backend.list_sessions()
        assert {item["session_id"] for item in everything} >= {
            session["session_id"] for session in [*running, completed]
        }


class TestLazyTableResolution:
    def test_the_table_name_comes_from_the_published_parameter(self, missions_table, monkeypatch):
        from mission.state import DynamoDBBackend

        monkeypatch.setenv("GCO_PROJECT_NAME", missions_table["project"])
        backend = DynamoDBBackend()
        session = _session(unique_name("sess-ssm"), created_at="2026-09-12T12:00:00Z")

        backend.save_session(session)

        assert backend._table_name == missions_table["table_name"]
        assert (
            DynamoDBBackend(table_name=missions_table["table_name"]).load_session(
                session["session_id"]
            )
            == session
        )
