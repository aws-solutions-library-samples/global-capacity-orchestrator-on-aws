"""Floci-backed integration tests for the vector-ingest Lambda.

The handler under test is the production module from
``lambda/vector-ingest/handler.py``, loaded exactly as Lambda would load
it. Its S3 and DynamoDB clients are the real boto3 clients it builds for
itself — the session environment applied by ``verified_floci_endpoint``
routes them to the emulator, so ``get_object`` and ``put_item`` travel
the genuine wire protocol against real service state. Only the Bedrock
client is replaced (Floci does not emulate Bedrock): a deterministic
fixed-width embedder, mirroring ``tests/test_floci_mission_memory.py``.

The emulator cannot create DynamoDB vector indexes, so the ingest table
here is the plain-key shape (``doc_id`` S HASH) — which is exactly what
the write path sees anyway: ``put_item`` is index-agnostic. The final
test pins that gap: the ``UpdateTable`` vector-index call the stack's
custom resource makes must be rejected by the emulator. If a Floci
release starts accepting it, that test fails loudly — the signal to
grow real index + SearchVectors coverage here.

See docs/FLOCI_TESTING.md for the layer map.
"""

from __future__ import annotations

import hashlib
import io
import json

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


@pytest.fixture()
def corpus_bucket(s3):
    bucket = unique_name("gco-cluster-shared")
    s3.create_bucket(Bucket=bucket)
    yield bucket
    listed = s3.list_objects_v2(Bucket=bucket)
    for entry in listed.get("Contents", []):
        s3.delete_object(Bucket=bucket, Key=entry["Key"])
    s3.delete_bucket(Bucket=bucket)


@pytest.fixture()
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


@pytest.fixture()
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
        with pytest.raises(RuntimeError, match="ghost.md"):
            handler.lambda_handler(_event(corpus_bucket, f"{_PREFIX}ghost.md"), context=None)


class TestVectorIndexGap:
    """Pin the emulator's exact vector-index gap (Floci 1.6.0, probed live).

    ``UpdateTable`` + ``VectorIndexUpdates`` is ACCEPTED but silently
    dropped — ``DescribeTable`` shows no index afterwards — and
    ``SearchVectors`` answers a typed ``UnknownOperationException``.
    While both hold, running the ingest tests against a plain-keyed
    table is sound (``put_item`` is index-agnostic) and the query side
    stays out of emulator scope. If either test fails after a Floci
    bump, the gap closed: grow real index + ``SearchVectors`` coverage
    here and retire these pins.
    """

    def test_index_create_is_silently_dropped_not_materialized(self, dynamodb, store_table):
        # The exact UpdateTable shape the global stack's custom resource
        # issues — the emulator swallows the unknown member.
        dynamodb.update_table(
            TableName=store_table,
            AttributeDefinitions=[{"AttributeName": "source", "AttributeType": "S"}],
            VectorIndexUpdates=[
                {
                    "Create": {
                        "IndexName": "corpus-embedding-index",
                        "VectorAttribute": {"AttributeName": "embedding"},
                        "Dimensions": _DIMENSIONS,
                        "DistanceFunction": "COSINE",
                        "SearchSchema": [
                            {
                                "AttributeName": "source",
                                "SearchSchemaElementType": "INLINE_FILTER",
                            }
                        ],
                        "Projection": {
                            "ProjectionType": "INCLUDE",
                            "NonKeyAttributes": ["text", "source"],
                        },
                    }
                }
            ],
        )

        description = dynamodb.describe_table(TableName=store_table)["Table"]
        assert "VectorIndexes" not in description

    def test_search_vectors_answers_a_typed_unknown_operation(self, dynamodb, store_table):
        with pytest.raises(ClientError) as exc_info:
            dynamodb.search_vectors(
                TableName=store_table,
                IndexName="corpus-embedding-index",
                SearchVector=[{"N": repr(component)} for component in _VECTOR],
                TopK=1,
            )
        assert exc_info.value.response["Error"]["Code"] == "UnknownOperationException"
