"""Floci emulator tests for the mission-memory store's plain-DynamoDB paths.

Opt-in via ``GCO_FLOCI_ENDPOINT`` (see ``docs/FLOCI_TESTING.md``). The
emulator does not implement ``SearchVectors``, so the similarity-search
path stays covered by mocked-boto3 unit tests and an AWS-credentialed
smoke test — but the write and list paths are ordinary DynamoDB
operations, and exercising them over the real wire protocol validates
what no client-side fake can:

* the hand-rolled typed-attribute serialisation in ``write_memory``
  (``L``-of-``N`` embedding vectors — including scientific-notation
  number strings from ``repr(float)`` — TTL numbers, string lists) is
  accepted by a real request parser and round-trips;
* ``PutItem`` overwrite semantics on ``session_id``, which is the
  idempotency claim ``gco mission memory backfill`` makes;
* ``list_memories``' ``ProjectionExpression`` is valid expression
  syntax server-side and actually suppresses the embedding vector;
* the multi-page ``Scan`` loop follows real ``LastEvaluatedKey``
  cursors rather than assuming one page.

The final test pins the gap itself: ``search_similar`` against an
environment without ``SearchVectors`` must surface a typed
``MissionMemoryError`` — never a raw botocore exception.

The store builds its own boto3 client; the session-scoped
``verified_floci_endpoint`` fixture routes it to the emulator through
``AWS_ENDPOINT_URL`` with throwaway credentials, exactly like the
sibling store tests in ``test_floci_dynamodb_stores.py``.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path
from typing import Any

import boto3
import pytest

from tests._floci import floci_test_markers, unique_name

# Mirror the import pattern used by the other ``test_mission_*`` modules.
sys.path.insert(0, str(Path(__file__).parent.parent / "gco_mcp"))
from mission import memory as memory_module  # noqa: E402
from mission.memory import (  # noqa: E402
    MEMORY_SCHEMA_VERSION,
    MissionMemoryError,
    MissionMemoryStore,
)

pytestmark = floci_test_markers()

#: Small fixed-width test vectors. ``1e-08`` is deliberate: ``repr(float)``
#: renders it in scientific notation, and only a real wire parser can prove
#: DynamoDB's number grammar accepts that form.
_DIMENSIONS = 3
_VECTOR = [0.5, -0.25, 1e-08]


@pytest.fixture(scope="module")
def dynamodb(verified_floci_endpoint: str):
    return boto3.client("dynamodb")


@pytest.fixture()
def store(dynamodb, monkeypatch: pytest.MonkeyPatch):
    """A ``MissionMemoryStore`` against a fresh emulator table.

    The table is shaped exactly like ``gco/stacks/global_stack.py``
    provisions it (``session_id`` S partition key, on-demand billing) —
    minus the vector index, which the emulator cannot create. Bedrock is
    not part of Floci, so the embedder is stubbed with a deterministic
    fixed-width vector; everything DynamoDB-facing is real.
    """
    table_name = unique_name("gco-mission-memory")
    dynamodb.create_table(
        TableName=table_name,
        KeySchema=[{"AttributeName": "session_id", "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": "session_id", "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST",
    )
    dynamodb.get_waiter("table_exists").wait(TableName=table_name)
    monkeypatch.setattr(memory_module, "embed_text", lambda text, **_: list(_VECTOR))
    yield MissionMemoryStore(
        table_name,
        "directive-embedding-index",
        embedding_model_id="floci-embed-model",
        dimensions=_DIMENSIONS,
    )
    dynamodb.delete_table(TableName=table_name)


def _session(session_id: str, *, completed_at: str = "2026-08-10T12:34:56+00:00") -> dict[str, Any]:
    return {
        "session_id": session_id,
        "directive_text": f"directive for {session_id}",
        "criteria": [{"criterion_id": "c1", "kind": "metric_threshold"}],
        "tool_allowlist": ["find_examples"],
        "created_at": "2026-08-10T12:00:00+00:00",
        "ended_at": completed_at,
        "iterations": [{}, {}],
    }


class TestWritePath:
    def test_item_round_trips_with_wire_types(self, store, dynamodb):
        before = int(time.time())
        store.write_memory(
            _session("sess-floci-001"),
            "complete",
            "criteria_met",
            "Lessons paragraph.",
            ["Follow up A"],
        )

        raw = dynamodb.get_item(
            TableName=store._table_name, Key={"session_id": {"S": "sess-floci-001"}}
        )["Item"]

        # The embedding survived as L-of-N, including the
        # scientific-notation component — compare numerically because the
        # server may normalise the number's string representation.
        stored_vector = [float(entry["N"]) for entry in raw["directive_embedding"]["L"]]
        assert stored_vector == pytest.approx(_VECTOR)
        assert raw["directive"] == {"S": "directive for sess-floci-001"}
        assert raw["lessons"] == {"S": "Lessons paragraph."}
        assert raw["recommended_followups"] == {"L": [{"S": "Follow up A"}]}
        assert raw["final_verdict"] == {"S": "complete"}
        assert raw["verdict_reason"] == {"S": "criteria_met"}
        assert raw["iteration_count"] == {"N": "2"}
        assert raw["criteria_summary"] == {"L": [{"S": "c1"}]}
        assert raw["schema_version"] == {"S": MEMORY_SCHEMA_VERSION}
        assert raw["embedding_model_id"] == {"S": "floci-embed-model"}
        assert raw["embedding_dimensions"] == {"N": str(_DIMENSIONS)}
        assert int(raw["ttl"]["N"]) >= before  # TTL is epoch seconds in the future

    def test_rewrite_overwrites_not_duplicates(self, store, dynamodb):
        # The idempotency claim `gco mission memory backfill` makes:
        # writes are keyed on session_id and simply overwrite.
        store.write_memory(_session("sess-floci-002"), "complete", "criteria_met", "v1", [])
        store.write_memory(_session("sess-floci-002"), "terminate", "max_iterations", "v2", [])

        scan = dynamodb.scan(TableName=store._table_name)
        assert scan["Count"] == 1
        (item,) = scan["Items"]
        assert item["lessons"] == {"S": "v2"}
        assert item["final_verdict"] == {"S": "terminate"}


class TestListPath:
    def test_orders_newest_first_and_honours_limit(self, store):
        for index, completed in enumerate(
            ["2026-07-01T00:00:00+00:00", "2026-08-03T00:00:00+00:00", "2026-07-15T00:00:00+00:00"]
        ):
            store.write_memory(
                _session(f"sess-order-{index}", completed_at=completed),
                "complete",
                "criteria_met",
                "L",
                [],
            )

        listed = store.list_memories()
        assert [m["session_id"] for m in listed] == ["sess-order-1", "sess-order-2", "sess-order-0"]

        capped = store.list_memories(limit=2)
        assert [m["session_id"] for m in capped] == ["sess-order-1", "sess-order-2"]

    def test_projection_suppresses_the_vector_server_side(self, store, dynamodb):
        store.write_memory(_session("sess-proj"), "complete", "criteria_met", "big lessons", [])

        raw = dynamodb.get_item(
            TableName=store._table_name, Key={"session_id": {"S": "sess-proj"}}
        )["Item"]
        assert "directive_embedding" in raw  # it IS on the item...

        (summary,) = store.list_memories()
        assert "directive_embedding" not in summary  # ...and never leaves DynamoDB here
        assert "lessons" not in summary  # summaries are slim; search returns lessons
        assert summary["session_id"] == "sess-proj"
        assert summary["iteration_count"] == 2  # Decimal -> int through the real wire

    def test_follows_real_scan_pagination_cursors(self, store):
        # A >1MB scan page cannot be faked client-side: the 1MB limit
        # applies to the data scanned (pre-projection), so bulky lessons
        # force multiple pages even though the projection is slim.
        payload = "x" * (64 * 1024)
        for index in range(25):
            store.write_memory(
                _session(f"sess-page-{index:03d}"), "complete", "criteria_met", payload, []
            )

        listed = store.list_memories(limit=50)
        assert len(listed) == 25
        assert {m["session_id"] for m in listed} == {f"sess-page-{i:03d}" for i in range(25)}


class TestSearchVectorsGap:
    def test_search_surfaces_a_typed_error_not_a_raw_exception(self, store):
        """The emulator has no ``SearchVectors``; the store must map that.

        If a future Floci release implements the API, this fails loudly —
        the signal to replace the mocked search coverage with real
        emulator coverage and retire the skip-marked placeholder in
        ``tests/test_mission_memory_runtime.py``.
        """
        with pytest.raises(MissionMemoryError):
            store.search_similar("anything at all")
