"""Floci-backed integration tests for the vector-ingest Lambda.

The handler under test is the production module from
``lambda/vector-ingest/handler.py``, loaded exactly as Lambda would load
it. Its S3 and DynamoDB clients are the real boto3 clients it builds for
itself — the session environment applied by ``verified_floci_endpoint``
routes them to the emulator, so ``get_object`` and ``put_item`` travel
the genuine wire protocol against real service state. Only the Bedrock
client is replaced (Floci does not emulate Bedrock): a deterministic
fixed-width embedder, mirroring ``tests/test_floci_mission_memory.py``.

The write-path tests use the plain-key table shape (``doc_id`` S HASH):
``put_item`` is index-agnostic, and a plain table needs no index build.
Since Floci 2.2.0 the emulator also materializes DynamoDB vector indexes
and serves ``SearchVectors``, so the final class creates the index with
the exact ``UpdateTable`` shape the global stack's custom resource issues
and proves what the handler writes is what a search returns: the
``INCLUDE`` projection, the inline ``source`` filter, and the
``ValidationException`` a still-backfilling index answers.

See docs/FLOCI_TESTING.md for the layer map.
"""

from __future__ import annotations

import hashlib
import io
import json
import re
import time

import boto3
import pytest
from botocore.exceptions import ClientError

from tests._floci import floci_test_markers, unique_name
from tests._lambda_imports import load_lambda_module

pytestmark = floci_test_markers()

#: Fixed-width test vector; ``1e-08`` is deliberate — ``repr(float)``
#: renders it in scientific notation, and only a real wire parser can
#: prove DynamoDB's number grammar accepts that form.
_DIMENSIONS = 3
_VECTOR = [0.5, -0.25, 1e-08]
_MODEL = "floci-embed-model"
_PREFIX = "vector-corpus/"


class _FixedBedrock:
    """Deterministic stand-in for the one service Floci does not carry."""

    def invoke_model(self, modelId, body, contentType, accept):
        assert json.loads(body)["inputText"].strip()
        return {"body": io.BytesIO(json.dumps({"embedding": list(_VECTOR)}).encode())}


@pytest.fixture(scope="module")
def s3(verified_floci_endpoint: str):
    return boto3.client("s3")


@pytest.fixture(scope="module")
def dynamodb(verified_floci_endpoint: str):
    return boto3.client("dynamodb")


@pytest.fixture
def corpus_bucket(s3):
    bucket = unique_name("gco-cluster-shared")
    s3.create_bucket(Bucket=bucket)
    yield bucket
    listed = s3.list_objects_v2(Bucket=bucket)
    for entry in listed.get("Contents", []):
        s3.delete_object(Bucket=bucket, Key=entry["Key"])
    s3.delete_bucket(Bucket=bucket)


@pytest.fixture
def store_table(dynamodb):
    """A vector-store-shaped table, minus the index the emulator lacks."""
    table_name = unique_name("gco-vector-store")
    dynamodb.create_table(
        TableName=table_name,
        KeySchema=[{"AttributeName": "doc_id", "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": "doc_id", "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST",
    )
    dynamodb.get_waiter("table_exists").wait(TableName=table_name)
    yield table_name
    dynamodb.delete_table(TableName=table_name)


@pytest.fixture
def handler(monkeypatch, store_table):
    """The production handler wired to the emulator, Bedrock stubbed."""
    module = load_lambda_module("vector-ingest")
    monkeypatch.setenv("VECTOR_STORE_TABLE_NAME", store_table)
    monkeypatch.setenv("EMBEDDING_MODEL_ID", _MODEL)
    monkeypatch.setenv("EMBEDDING_DIMENSIONS", str(_DIMENSIONS))
    monkeypatch.setenv("CORPUS_PREFIX", _PREFIX)
    module._bedrock_client = _FixedBedrock()
    return module


def _event(bucket: str, *keys: str) -> dict:
    return {
        "Records": [{"s3": {"bucket": {"name": bucket}, "object": {"key": key}}} for key in keys]
    }


class TestIngestOverTheRealWire:
    def test_markdown_object_round_trips_with_wire_types(
        self, handler, s3, dynamodb, corpus_bucket, store_table
    ):
        key = f"{_PREFIX}guides/intro.md"
        content = "# Intro Guide\n\nA paragraph about capacity."
        s3.put_object(Bucket=corpus_bucket, Key=key, Body=content.encode())

        summary = handler.lambda_handler(_event(corpus_bucket, key), context=None)

        assert summary["ingested_objects"] == 1
        assert summary["failures"] == []
        doc_id = hashlib.sha256(key.encode()).hexdigest()[:16] + "#0000"
        raw = dynamodb.get_item(TableName=store_table, Key={"doc_id": {"S": doc_id}})["Item"]
        # The embedding survived as L-of-N — including the scientific-
        # notation component; compare numerically because the server may
        # normalise the number's string rendering.
        stored_vector = [float(entry["N"]) for entry in raw["embedding"]["L"]]
        assert stored_vector == pytest.approx(_VECTOR)
        assert raw["text"] == {"S": "# Intro Guide\n\nA paragraph about capacity."}
        assert raw["source"] == {"S": key}
        assert raw["chunk_index"] == {"N": "0"}
        assert raw["title"] == {"S": "Intro Guide"}
        assert raw["embedding_model_id"] == {"S": _MODEL}
        assert raw["content_sha256"] == {"S": hashlib.sha256(content.encode()).hexdigest()}

    def test_redelivery_overwrites_not_duplicates(
        self, handler, s3, dynamodb, corpus_bucket, store_table
    ):
        # S3 event delivery is at-least-once; deterministic doc_ids make
        # the second delivery a pure overwrite.
        key = f"{_PREFIX}notes.txt"
        s3.put_object(Bucket=corpus_bucket, Key=key, Body=b"one\n\ntwo")

        handler.lambda_handler(_event(corpus_bucket, key), context=None)
        handler.lambda_handler(_event(corpus_bucket, key), context=None)

        assert dynamodb.scan(TableName=store_table)["Count"] == 1

    def test_jsonl_records_land_as_separate_items(
        self, handler, s3, dynamodb, corpus_bucket, store_table
    ):
        key = f"{_PREFIX}records.jsonl"
        body = '{"text": "alpha", "title": "A"}\n{"text": "beta"}\n'
        s3.put_object(Bucket=corpus_bucket, Key=key, Body=body.encode())

        summary = handler.lambda_handler(_event(corpus_bucket, key), context=None)

        assert summary["ingested_chunks"] == 2
        scan = dynamodb.scan(TableName=store_table)
        assert scan["Count"] == 2
        by_index = {item["chunk_index"]["N"]: item for item in scan["Items"]}
        assert by_index["0"]["title"] == {"S": "A"}
        assert "title" not in by_index["1"]

    def test_missing_object_fails_that_object_over_the_real_wire(self, handler, corpus_bucket):
        # The real emulator answers NoSuchKey; per-object isolation turns
        # it into a summary failure and a batch-level raise.
        with pytest.raises(RuntimeError, match=re.escape("ghost.md")):
            handler.lambda_handler(_event(corpus_bucket, f"{_PREFIX}ghost.md"), context=None)


#: The stack's index (gco/stacks/global_stack.py ``VectorStoreIndex``),
#: field for field, at this module's test width.
_INDEX_NAME = "corpus-embedding-index"
_PROJECTED = ["text", "source", "chunk_index", "title", "embedding_model_id"]
_STACK_INDEX_UPDATE = {
    "Create": {
        "IndexName": _INDEX_NAME,
        "VectorAttribute": {"AttributeName": "embedding"},
        "Dimensions": _DIMENSIONS,
        "DistanceFunction": "COSINE",
        "SearchSchema": [{"AttributeName": "source", "SearchSchemaElementType": "INLINE_FILTER"}],
        "Projection": {"ProjectionType": "INCLUDE", "NonKeyAttributes": list(_PROJECTED)},
    }
}
#: Upper bound for an index build. Floci's defaults are 4 s of allocation
#: plus 10 s of backfill (FLOCI_SERVICES_DYNAMODB_VECTOR_INDEX_*_SECONDS).
_INDEX_BUILD_TIMEOUT_SECONDS = 90


def _create_stack_index(dynamodb, table_name: str) -> None:
    dynamodb.update_table(
        TableName=table_name,
        AttributeDefinitions=[{"AttributeName": "source", "AttributeType": "S"}],
        VectorIndexUpdates=[_STACK_INDEX_UPDATE],
    )


def _index(dynamodb, table_name: str) -> dict:
    (index,) = dynamodb.describe_table(TableName=table_name)["Table"]["VectorIndexes"]
    return index


def _wait_for_active_index(dynamodb, table_name: str) -> dict:
    deadline = time.monotonic() + _INDEX_BUILD_TIMEOUT_SECONDS
    while True:
        index = _index(dynamodb, table_name)
        if index["IndexStatus"] == "ACTIVE":
            return index
        if time.monotonic() > deadline:
            pytest.fail(
                f"vector index still {index['IndexStatus']} after {_INDEX_BUILD_TIMEOUT_SECONDS}s"
            )
        time.sleep(1)


def _search(dynamodb, table_name: str, **kwargs) -> list[dict]:
    response = dynamodb.search_vectors(
        TableName=table_name,
        IndexName=_INDEX_NAME,
        SearchVector=[{"N": repr(component)} for component in _VECTOR],
        TopK=5,
        **kwargs,
    )
    return response["SearchResults"]


@pytest.fixture(scope="module")
def indexed_table(dynamodb):
    """A store table carrying the stack's vector index, built once per module."""
    table_name = unique_name("gco-vector-indexed")
    dynamodb.create_table(
        TableName=table_name,
        KeySchema=[{"AttributeName": "doc_id", "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": "doc_id", "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST",
    )
    dynamodb.get_waiter("table_exists").wait(TableName=table_name)
    _create_stack_index(dynamodb, table_name)
    _wait_for_active_index(dynamodb, table_name)
    yield table_name
    dynamodb.delete_table(TableName=table_name)


@pytest.fixture
def indexed_handler(monkeypatch, indexed_table):
    module = load_lambda_module("vector-ingest")
    monkeypatch.setenv("VECTOR_STORE_TABLE_NAME", indexed_table)
    monkeypatch.setenv("EMBEDDING_MODEL_ID", _MODEL)
    monkeypatch.setenv("EMBEDDING_DIMENSIONS", str(_DIMENSIONS))
    monkeypatch.setenv("CORPUS_PREFIX", _PREFIX)
    module._bedrock_client = _FixedBedrock()
    return module


class TestVectorIndexOverTheRealWire:
    """The stack's index and ``SearchVectors``, against Floci 2.2.0.

    Earlier Floci releases accepted ``VectorIndexUpdates`` but dropped it and
    had no ``SearchVectors``; this module pinned that gap until 2.2.0 closed
    it. The search side now runs for real.
    """

    def test_the_stack_index_shape_materializes_with_its_declared_fields(
        self, dynamodb, indexed_table
    ):
        index = _index(dynamodb, indexed_table)

        assert index["IndexStatus"] == "ACTIVE"
        assert index["VectorAttribute"] == {"AttributeName": "embedding"}
        assert index["Dimensions"] == _DIMENSIONS
        assert index["DistanceFunction"] == "COSINE"
        assert index["Projection"] == {"ProjectionType": "INCLUDE", "NonKeyAttributes": _PROJECTED}

    def test_an_ingested_chunk_is_searchable_through_the_projection(
        self, indexed_handler, s3, dynamodb, corpus_bucket, indexed_table
    ):
        key = f"{_PREFIX}search/guide.md"
        s3.put_object(Bucket=corpus_bucket, Key=key, Body=b"# Guide\n\nCapacity notes.")
        indexed_handler.lambda_handler(_event(corpus_bucket, key), context=None)

        results = _search(
            dynamodb,
            indexed_table,
            SearchConditionExpression="#source = :source",
            ExpressionAttributeNames={"#source": "source"},
            ExpressionAttributeValues={":source": {"S": key}},
        )

        (hit,) = results
        # The INCLUDE projection plus the key, and nothing else: the vector
        # and the provenance hash stay out of search responses.
        assert set(hit["Item"]) == {"doc_id", *_PROJECTED}
        assert hit["Item"]["source"] == {"S": key}
        assert hit["Item"]["title"] == {"S": "Guide"}
        assert hit["Item"]["embedding_model_id"] == {"S": _MODEL}
        # Identical vectors: COSINE distance is zero up to rounding.
        assert float(hit["Score"]) == pytest.approx(0.0, abs=1e-9)

    def test_the_inline_source_filter_narrows_the_ranking(
        self, indexed_handler, s3, dynamodb, corpus_bucket, indexed_table
    ):
        keys = [f"{_PREFIX}filter/a.txt", f"{_PREFIX}filter/b.txt"]
        for key in keys:
            s3.put_object(Bucket=corpus_bucket, Key=key, Body=b"same text")
        indexed_handler.lambda_handler(_event(corpus_bucket, *keys), context=None)

        filtered = _search(
            dynamodb,
            indexed_table,
            SearchConditionExpression="#source = :source",
            ExpressionAttributeNames={"#source": "source"},
            ExpressionAttributeValues={":source": {"S": keys[1]}},
        )

        assert [hit["Item"]["source"]["S"] for hit in filtered] == [keys[1]]

    def test_a_backfilling_index_answers_validation_exception(self, dynamodb, store_table):
        # What the CLI maps to "the index may still be building": a search
        # issued right after the index is created, before it is ACTIVE.
        _create_stack_index(dynamodb, store_table)
        try:
            assert _index(dynamodb, store_table)["IndexStatus"] == "CREATING"
            with pytest.raises(ClientError) as exc_info:
                _search(dynamodb, store_table)
            assert exc_info.value.response["Error"]["Code"] == "ValidationException"
        finally:
            # A table cannot be deleted while an index builds; let the
            # function-scoped fixture's teardown find it ACTIVE.
            _wait_for_active_index(dynamodb, store_table)
