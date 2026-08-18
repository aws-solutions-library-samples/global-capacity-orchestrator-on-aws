"""Floci-backed integration tests for the ``gco vector`` CLI core.

:class:`cli.vector_store.VectorStoreClient` runs here exactly as it does
for an operator: SSM discovery, S3 corpus uploads, and DynamoDB reads
all travel the real wire protocol against the emulator — the session
environment applied by ``verified_floci_endpoint`` routes the client's
own lazily built boto3 clients. Only Bedrock is replaced (Floci does not
emulate it), mirroring ``tests/test_floci_vector_ingest.py``.

Each test namespaces its SSM parameters under a unique project name
(``GCO_PROJECT_NAME``), so the discovery path — not just the data path —
is exercised for real, including the ParameterNotFound → unavailable
mapping an operator hits on a deployment without the feature.

The search gap pin matches the ingest module's: the emulator answers
``UnknownOperationException`` for ``SearchVectors``, which the client
must surface as a typed error, not a raw traceback. When a Floci release
implements the API, that pin fails — the signal to grow real similarity
coverage here.
"""

from __future__ import annotations

import io
import json

import boto3
import pytest

from cli.vector_store import (
    VectorStoreClient,
    VectorStoreError,
    VectorStoreUnavailableError,
)
from tests._floci import floci_test_markers, unique_name

pytestmark = floci_test_markers()

_DIMENSIONS = 3
_VECTOR = [0.5, -0.25, 1e-08]


class _FixedBedrock:
    """Deterministic stand-in for the one service Floci does not carry."""

    def invoke_model(self, modelId, body, contentType, accept):
        assert json.loads(body)["inputText"].strip()
        return {"body": io.BytesIO(json.dumps({"embedding": list(_VECTOR)}).encode())}


@pytest.fixture(scope="module")
def dynamodb(verified_floci_endpoint: str):
    return boto3.client("dynamodb")


@pytest.fixture(scope="module")
def s3(verified_floci_endpoint: str):
    return boto3.client("s3")


@pytest.fixture(scope="module")
def ssm(verified_floci_endpoint: str):
    return boto3.client("ssm")


@pytest.fixture()
def deployment(dynamodb, s3, ssm, monkeypatch):
    """A vector-store deployment's discoverable surface, minus the index.

    Publishes the four SSM parameters the client resolves, a corpus
    bucket, and a plain-keyed table (the emulator cannot create vector
    indexes; ``put_item``/``scan`` are index-agnostic). Returns the
    project name the client discovers everything through.
    """
    project = unique_name("gco-vcli")
    table_name = f"{project}-vector-store"
    bucket_name = f"{project}-cluster-shared"

    monkeypatch.setenv("GCO_PROJECT_NAME", project)
    dynamodb.create_table(
        TableName=table_name,
        KeySchema=[{"AttributeName": "doc_id", "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": "doc_id", "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST",
    )
    dynamodb.get_waiter("table_exists").wait(TableName=table_name)
    s3.create_bucket(Bucket=bucket_name)
    parameters = {
        f"/{project}/vector-store-table-name": table_name,
        f"/{project}/vector-store-index-name": "corpus-embedding-index",
        f"/{project}/cluster-shared-bucket/name": bucket_name,
        f"/{project}/cluster-shared-bucket/region": "us-east-1",
    }
    for name, value in parameters.items():
        ssm.put_parameter(Name=name, Value=value, Type="String")

    yield {"project": project, "table": table_name, "bucket": bucket_name}

    for name in parameters:
        ssm.delete_parameter(Name=name)
    listed = s3.list_objects_v2(Bucket=bucket_name)
    for entry in listed.get("Contents", []):
        s3.delete_object(Bucket=bucket_name, Key=entry["Key"])
    s3.delete_bucket(Bucket=bucket_name)
    dynamodb.delete_table(TableName=table_name)


def _client() -> VectorStoreClient:
    client = VectorStoreClient(dimensions=_DIMENSIONS)
    client._bedrock_client = _FixedBedrock()
    return client


class TestDiscoveryAndIngestOverTheRealWire:
    def test_ingest_resolves_names_from_ssm_and_uploads(self, deployment, s3, tmp_path):
        doc = tmp_path / "intro.md"
        doc.write_text("# Intro\n\nA paragraph about capacity.")

        summary = _client().ingest([doc])

        assert summary["bucket"] == deployment["bucket"]
        assert summary["uploaded"] == ["vector-corpus/intro.md"]
        stored = s3.get_object(Bucket=deployment["bucket"], Key="vector-corpus/intro.md")
        assert stored["Body"].read() == doc.read_bytes()

    def test_ingest_wait_counts_chunks_with_a_real_filtered_scan(
        self, deployment, dynamodb, tmp_path
    ):
        # Simulate the ingest Lambda's output for the uploaded key, then
        # let the wait path count it through a genuine server-side
        # FilterExpression ("source" needs the attribute-name alias; only
        # a real parser proves the expression is valid).
        doc = tmp_path / "notes.txt"
        doc.write_text("text")
        dynamodb.put_item(
            TableName=deployment["table"],
            Item={
                "doc_id": {"S": "aaaa#0000"},
                "source": {"S": "vector-corpus/notes.txt"},
                "embedding": {"L": [{"N": repr(v)} for v in _VECTOR]},
            },
        )

        summary = _client().ingest([doc], wait_timeout_seconds=30)

        assert summary["timed_out"] is False
        assert summary["chunks_by_source"] == {"vector-corpus/notes.txt": 1}

    def test_status_reads_the_real_table_description(self, deployment):
        status = _client().status()

        assert status["table_name"] == deployment["table"]
        assert status["table_status"] == "ACTIVE"
        # The emulator cannot materialize vector indexes (pinned in
        # tests/test_floci_vector_ingest.py), so the defensive describe
        # walk must degrade to NOT_VISIBLE — never a KeyError.
        assert status["index_status"] == "NOT_VISIBLE"

    def test_a_missing_deployment_maps_to_unavailable(self, monkeypatch):
        monkeypatch.setenv("GCO_PROJECT_NAME", unique_name("gco-absent"))

        with pytest.raises(VectorStoreUnavailableError, match="vector_store.enabled"):
            _client().status()


class TestSearchVectorsGap:
    def test_search_surfaces_a_typed_error_not_a_raw_exception(self, deployment):
        """The emulator has no ``SearchVectors``; the client must map that."""
        with pytest.raises(VectorStoreError, match="search failed"):
            _client().search("anything at all")
