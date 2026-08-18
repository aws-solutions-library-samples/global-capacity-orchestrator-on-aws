# Tests for the gco vector CLI: the VectorStoreClient core
# (cli/vector_store.py) with stubbed AWS clients, the click veneer
# (cli/commands/vector_cmd.py) via CliRunner, the defaults-vs-cdk.json
# agreement, and the cross-implementation Titan embedding contract that
# keeps query vectors and corpus vectors from ever diverging in request
# shape.

import hashlib
import io
import json
from pathlib import Path

import pytest
from botocore.exceptions import ClientError, EndpointConnectionError
from click.testing import CliRunner

import cli.vector_store as vector_store_module
from cli.main import cli
from cli.vector_store import (
    DEFAULT_CORPUS_PREFIX,
    DEFAULT_DIMENSIONS,
    DEFAULT_EMBEDDING_MODEL_ID,
    VectorStoreClient,
    VectorStoreError,
    VectorStoreUnavailableError,
    demo_corpus_paths,
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent

_DIMS = 4
_MODEL = "amazon.titan-embed-text-v2:0"


class _FakeBedrock:
    def __init__(self, dims=_DIMS):
        self.dims = dims
        self.bodies = []

    def invoke_model(self, modelId, body, contentType, accept):
        self.bodies.append(json.loads(body))
        digest = hashlib.sha256(self.bodies[-1]["inputText"].encode()).digest()
        vector = [digest[i] / 255.0 for i in range(self.dims)]
        return {"body": io.BytesIO(json.dumps({"embedding": vector}).encode())}


class _FakeDynamo:
    def __init__(self, search_response=None, describe=None, scan_pages=None, error=None):
        self.search_response = search_response or {"SearchResults": []}
        self.describe = describe
        self.scan_pages = list(scan_pages or [])
        self.error = error
        self.search_requests = []
        self.scan_requests = []

    def search_vectors(self, **request):
        self.search_requests.append(request)
        if self.error is not None:
            raise self.error
        return self.search_response

    def describe_table(self, TableName):
        if self.error is not None:
            raise self.error
        return {"Table": self.describe}

    def scan(self, **request):
        self.scan_requests.append(request)
        if self.error is not None:
            raise self.error
        return self.scan_pages.pop(0) if self.scan_pages else {"Count": 0}


class _FakeS3:
    def __init__(self):
        self.puts = []

    def put_object(self, Bucket, Key, Body):
        self.puts.append((Bucket, Key, Body))


def _client(**kwargs):
    """A client with names injected so SSM is never consulted."""
    defaults = {
        "table_name": "gco-vector-store",
        "index_name": "corpus-embedding-index",
        "bucket_name": "shared-bucket",
        "dimensions": _DIMS,
    }
    defaults.update(kwargs)
    client = VectorStoreClient(**defaults)
    client._bedrock_client = _FakeBedrock(dims=int(defaults["dimensions"]))
    return client


def _client_error(code):
    return ClientError({"Error": {"Code": code, "Message": code}}, "op")


class TestSearch:
    def test_request_shape_and_response_parse(self):
        store = _client()
        store._dynamodb_client = _FakeDynamo(
            search_response={
                "SearchResults": [
                    {
                        "Item": {
                            "doc_id": {"S": "abc#0000"},
                            "text": {"S": "hello"},
                            "source": {"S": "vector-corpus/a.md"},
                            "chunk_index": {"N": "0"},
                            "embedding": {"L": [{"N": "0.5"}]},
                        },
                        "Score": 0.125,
                    }
                ]
            }
        )

        results = store.search("query text", top_k=2)

        (request,) = store._dynamodb_client.search_requests
        assert request["TableName"] == "gco-vector-store"
        assert request["IndexName"] == "corpus-embedding-index"
        assert request["TopK"] == 2
        # SearchVector is a PLAIN list of number attrs (the {"L": ...}
        # wrapper is a write-side shape) — live-verified in the spike.
        assert len(request["SearchVector"]) == _DIMS
        assert all(set(component) == {"N"} for component in request["SearchVector"])
        assert "SearchConditionExpression" not in request

        (item,) = results
        assert item["doc_id"] == "abc#0000"
        assert item["text"] == "hello"
        assert item["chunk_index"] == 0
        assert item["score"] == 0.125
        # The vector never reaches terminal output, even if a recreated
        # index projects it.
        assert "embedding" not in item

    def test_source_filter_rides_as_inline_condition(self):
        store = _client()
        store._dynamodb_client = _FakeDynamo()

        store.search("q", source="vector-corpus/a.md")

        (request,) = store._dynamodb_client.search_requests
        # ``source`` is a DynamoDB reserved keyword: the live service
        # rejects the bare name in a SearchConditionExpression, so the
        # filter must ride behind an ExpressionAttributeNames alias.
        assert request["SearchConditionExpression"] == "#source = :source"
        assert request["ExpressionAttributeNames"] == {"#source": "source"}
        assert request["ExpressionAttributeValues"] == {":source": {"S": "vector-corpus/a.md"}}

    @pytest.mark.parametrize("code", ["ValidationException", "ResourceNotFoundException"])
    def test_building_index_maps_to_unavailable(self, code):
        # SearchVectors answers ValidationException for several minutes
        # after first deploy while the index builds (spike finding); that
        # must read as "not there yet", not a hard failure.
        store = _client()
        store._dynamodb_client = _FakeDynamo(error=_client_error(code))

        with pytest.raises(VectorStoreUnavailableError, match=code):
            store.search("q")

    def test_other_client_errors_stay_hard_failures(self):
        store = _client()
        store._dynamodb_client = _FakeDynamo(error=_client_error("AccessDeniedException"))

        with pytest.raises(VectorStoreError, match="search failed") as exc_info:
            store.search("q")
        assert not isinstance(exc_info.value, VectorStoreUnavailableError)

    def test_endpoint_fault_maps_to_unavailable(self):
        store = _client()
        store._dynamodb_client = _FakeDynamo(
            error=EndpointConnectionError(endpoint_url="https://example.invalid")
        )

        with pytest.raises(VectorStoreUnavailableError, match="unreachable"):
            store.search("q")

    def test_rejects_nonpositive_top_k_before_any_call(self):
        store = _client()
        with pytest.raises(VectorStoreError, match="top_k"):
            store.search("q", top_k=0)

    def test_width_mismatch_is_a_hard_error_with_remediation(self):
        store = _client()
        store._bedrock_client = _FakeBedrock(dims=_DIMS + 1)
        with pytest.raises(VectorStoreError, match="one-way doors"):
            store.search("q")

    def test_empty_query_is_rejected(self):
        store = _client()
        with pytest.raises(VectorStoreError, match="non-empty"):
            store.search("   ")


class TestStatus:
    def test_reports_table_replicas_and_index(self):
        store = _client()
        store._dynamodb_client = _FakeDynamo(
            describe={
                "TableStatus": "ACTIVE",
                "ItemCount": 7,
                "Replicas": [
                    {"RegionName": "us-east-2", "ReplicaStatus": "ACTIVE"},
                    {"RegionName": "us-east-1", "ReplicaStatus": "CREATING"},
                ],
                "VectorIndexes": [
                    {"IndexName": "corpus-embedding-index", "IndexStatus": "CREATING"}
                ],
            }
        )

        status = store.status()

        assert status["table_name"] == "gco-vector-store"
        assert status["table_status"] == "ACTIVE"
        assert status["item_count"] == 7
        assert status["index_status"] == "CREATING"
        assert status["replicas"] == [
            {"region": "us-east-2", "status": "ACTIVE"},
            {"region": "us-east-1", "status": "CREATING"},
        ]

    def test_absent_index_key_degrades_to_not_visible(self):
        store = _client()
        store._dynamodb_client = _FakeDynamo(
            describe={"TableStatus": "ACTIVE", "ItemCount": 0, "Replicas": []}
        )

        assert store.status()["index_status"] == "NOT_VISIBLE"

    def test_missing_table_maps_to_unavailable(self):
        store = _client()
        store._dynamodb_client = _FakeDynamo(error=_client_error("ResourceNotFoundException"))

        with pytest.raises(VectorStoreUnavailableError, match="not found"):
            store.status()


class TestIngest:
    def test_uploads_under_the_corpus_prefix(self, tmp_path):
        doc = tmp_path / "guide.md"
        doc.write_text("# Guide\n\nBody.")
        store = _client()
        store._s3_client = _FakeS3()

        summary = store.ingest([doc])

        assert summary == {"bucket": "shared-bucket", "uploaded": ["vector-corpus/guide.md"]}
        ((bucket, key, body),) = store._s3_client.puts
        assert (bucket, key) == ("shared-bucket", "vector-corpus/guide.md")
        assert body == doc.read_bytes()

    def test_refuses_uningestible_suffixes_before_any_upload(self, tmp_path):
        doc = tmp_path / "diagram.png"
        doc.write_bytes(b"\x89PNG")
        store = _client()
        store._s3_client = _FakeS3()

        with pytest.raises(VectorStoreError, match="unsupported suffix"):
            store.ingest([doc])
        assert store._s3_client.puts == []

    def test_refuses_an_empty_batch(self):
        with pytest.raises(VectorStoreError, match="nothing to ingest"):
            _client().ingest([])

    def test_wait_polls_until_every_source_has_chunks(self, tmp_path, monkeypatch):
        doc = tmp_path / "a.txt"
        doc.write_text("text")
        store = _client()
        store._s3_client = _FakeS3()
        store._dynamodb_client = _FakeDynamo(scan_pages=[{"Count": 0}, {"Count": 3}])
        monkeypatch.setattr(vector_store_module.time, "sleep", lambda _s: None)

        summary = store.ingest([doc], wait_timeout_seconds=60)

        assert summary["chunks_by_source"] == {"vector-corpus/a.txt": 3}
        assert summary["timed_out"] is False
        (first, second) = store._dynamodb_client.scan_requests
        assert first["Select"] == "COUNT"
        assert first["ExpressionAttributeValues"] == {":source": {"S": "vector-corpus/a.txt"}}
        assert second == first

    def test_wait_times_out_honestly(self, tmp_path, monkeypatch):
        doc = tmp_path / "a.txt"
        doc.write_text("text")
        store = _client()
        store._s3_client = _FakeS3()
        store._dynamodb_client = _FakeDynamo(scan_pages=[])  # always Count 0
        monkeypatch.setattr(vector_store_module.time, "sleep", lambda _s: None)
        clock = iter(range(0, 10_000, 2))
        monkeypatch.setattr(vector_store_module.time, "monotonic", lambda: float(next(clock)))

        summary = store.ingest([doc], wait_timeout_seconds=5)

        assert summary["timed_out"] is True
        assert summary["chunks_by_source"] == {"vector-corpus/a.txt": 0}

    def test_wait_follows_scan_pagination_cursors(self, tmp_path, monkeypatch):
        doc = tmp_path / "a.txt"
        doc.write_text("text")
        store = _client()
        store._s3_client = _FakeS3()
        store._dynamodb_client = _FakeDynamo(
            scan_pages=[
                {"Count": 1, "LastEvaluatedKey": {"doc_id": {"S": "x"}}},
                {"Count": 2},
            ]
        )
        monkeypatch.setattr(vector_store_module.time, "sleep", lambda _s: None)

        summary = store.ingest([doc], wait_timeout_seconds=60)

        assert summary["chunks_by_source"] == {"vector-corpus/a.txt": 3}
        (_, second) = store._dynamodb_client.scan_requests
        assert second["ExclusiveStartKey"] == {"doc_id": {"S": "x"}}


class TestSsmResolution:
    def test_parameter_not_found_maps_to_unavailable_with_hint(self, monkeypatch):
        def _missing(name, *, region=None):
            raise _client_error("ParameterNotFound")

        monkeypatch.setattr("gco.services.aws_ssm.get_ssm_parameter", _missing)
        store = VectorStoreClient()

        with pytest.raises(VectorStoreUnavailableError, match="vector_store.enabled"):
            store.search("q")

    def test_names_resolve_once_and_are_cached(self, monkeypatch):
        calls = []

        def _resolver(name, *, region=None):
            calls.append(name)
            return f"resolved-{name.rsplit('/', 1)[-1]}"

        monkeypatch.setattr("gco.services.aws_ssm.get_ssm_parameter", _resolver)
        store = VectorStoreClient(dimensions=_DIMS)
        store._bedrock_client = _FakeBedrock()
        store._dynamodb_client = _FakeDynamo()

        store.search("q")
        store.search("q")

        assert calls == ["/gco/vector-store-table-name", "/gco/vector-store-index-name"]
        assert store._dynamodb_client.search_requests[0]["TableName"] == (
            "resolved-vector-store-table-name"
        )


class TestDefaultsAgreeWithCdkJson:
    def test_client_defaults_mirror_the_vector_store_block(self):
        # The cdk.json block drives the deployed index and ingest
        # pipeline; these defaults drive the query vectors sent to it.
        # dimensions and embedding_model_id are one-way doors, so the two
        # must never drift apart silently.
        payload = json.loads((PROJECT_ROOT / "cdk.json").read_text(encoding="utf-8"))
        block = payload["context"]["vector_store"]

        assert block["dimensions"] == DEFAULT_DIMENSIONS
        assert block["embedding_model_id"] == DEFAULT_EMBEDDING_MODEL_ID
        assert block["corpus_prefix"] == DEFAULT_CORPUS_PREFIX


class TestDemoCorpus:
    def test_demo_corpus_is_the_checkouts_docs(self):
        paths = demo_corpus_paths()
        assert paths, "a source checkout always carries docs/*.md"
        assert all(path.suffix == ".md" for path in paths)
        assert paths == sorted(paths)
        assert all(path.parent == PROJECT_ROOT / "docs" for path in paths)


class TestTitanContractAcrossImplementations:
    """Query and corpus vectors must share one request shape.

    Three implementations build Titan embedding request bodies: mission
    memory's ``embed_text`` (gco_mcp), the ingest Lambda, and this CLI.
    A drift between the CLI and the Lambda would make query vectors
    subtly incomparable to the stored corpus, so the contract is pinned
    here across all three.
    """

    @pytest.mark.parametrize(
        "model_id",
        [
            pytest.param("amazon.titan-embed-text-v2:0", id="v2-family-carries-dimensions"),
            pytest.param("amazon.titan-embed-text-v1", id="v1-family-gets-bare-body"),
        ],
    )
    def test_cli_and_ingest_lambda_build_identical_bodies(self, model_id):
        from tests._lambda_imports import load_lambda_module

        ingest_handler = load_lambda_module("vector-ingest")

        cli_body = vector_store_module._embedding_request_body("query", model_id, 1024)
        lambda_body = ingest_handler._embedding_request_body("query", model_id, 1024)

        assert json.loads(cli_body) == json.loads(lambda_body)

    def test_mission_memory_embedder_sends_the_same_v2_body(self, monkeypatch):
        import sys

        sys.path.insert(0, str(PROJECT_ROOT / "gco_mcp"))
        from mission import embeddings as embeddings_module

        captured = {}

        class _CapturingClient:
            def invoke_model(self, modelId, body, contentType, accept):
                captured["body"] = json.loads(body)
                return {"body": io.BytesIO(json.dumps({"embedding": [0.0] * 1024}).encode())}

        monkeypatch.setattr(embeddings_module, "_build_client", lambda: _CapturingClient())
        embeddings_module.embed_text(
            "query", model_id="amazon.titan-embed-text-v2:0", dimensions=1024
        )

        cli_body = vector_store_module._embedding_request_body(
            "query", "amazon.titan-embed-text-v2:0", 1024
        )
        assert captured["body"] == json.loads(cli_body)


class TestVectorCommands:
    def _invoke(self, *args):
        return CliRunner().invoke(cli, ["vector", *args])

    def test_status_json(self, monkeypatch):
        fake_status = {
            "table_name": "gco-vector-store",
            "index_name": "corpus-embedding-index",
            "region": "us-east-2",
            "table_status": "ACTIVE",
            "item_count": 3,
            "index_status": "ACTIVE",
            "replicas": [{"region": "us-east-2", "status": "ACTIVE"}],
        }

        class _Fake:
            def __init__(self, **kwargs):
                pass

            def status(self):
                return fake_status

        monkeypatch.setattr("cli.vector_store.VectorStoreClient", _Fake)
        result = self._invoke("status")

        assert result.exit_code == 0, result.output
        assert json.loads(result.output) == fake_status

    def test_status_unavailable_exits_one_with_hint(self, monkeypatch):
        class _Fake:
            def __init__(self, **kwargs):
                pass

            def status(self):
                raise VectorStoreUnavailableError("SSM parameter not found")

        monkeypatch.setattr("cli.vector_store.VectorStoreClient", _Fake)
        result = self._invoke("status")

        assert result.exit_code == 1
        assert "vector_store.enabled" in result.output
        assert "vector_store_unavailable" in result.output

    def test_search_forwards_options_and_renders_table(self, monkeypatch):
        captured = {}

        class _Fake:
            def __init__(self, **kwargs):
                captured["init"] = kwargs

            def search(self, query, top_k, source):
                captured["search"] = (query, top_k, source)
                return [
                    {
                        "doc_id": "abc#0000",
                        "text": "some text",
                        "source": "vector-corpus/a.md",
                        "chunk_index": 0,
                        "score": 0.125,
                    }
                ]

        monkeypatch.setattr("cli.vector_store.VectorStoreClient", _Fake)
        result = self._invoke(
            "search",
            "how do capacity blocks work",
            "--top-k",
            "2",
            "--source",
            "vector-corpus/a.md",
            "--region",
            "us-east-1",
            "--output",
            "table",
        )

        assert result.exit_code == 0, result.output
        assert captured["init"]["query_region"] == "us-east-1"
        assert captured["search"] == ("how do capacity blocks work", 2, "vector-corpus/a.md")
        assert "0.125" in result.output
        assert "vector-corpus/a.md" in result.output

    def test_search_json_envelope(self, monkeypatch):
        class _Fake:
            def __init__(self, **kwargs):
                pass

            def search(self, query, top_k, source):
                return []

        monkeypatch.setattr("cli.vector_store.VectorStoreClient", _Fake)
        result = self._invoke("search", "anything")

        assert result.exit_code == 0
        assert json.loads(result.output) == {"results": []}

    def test_ingest_requires_files_or_demo(self):
        result = self._invoke("ingest")
        assert result.exit_code == 2
        assert "vector_ingest_invalid_args" in result.output

    def test_ingest_rejects_demo_with_files(self, tmp_path):
        doc = tmp_path / "a.md"
        doc.write_text("x")
        result = self._invoke("ingest", str(doc), "--demo")
        assert result.exit_code == 2
        assert "--demo takes no FILES" in result.output

    def test_ingest_uploads_and_reports(self, tmp_path, monkeypatch):
        doc = tmp_path / "a.md"
        doc.write_text("# T\n\nbody")
        captured = {}

        class _Fake:
            def __init__(self, **kwargs):
                pass

            def ingest(self, paths, wait_timeout_seconds):
                captured["paths"] = paths
                captured["wait"] = wait_timeout_seconds
                return {"bucket": "b", "uploaded": ["vector-corpus/a.md"]}

        monkeypatch.setattr("cli.vector_store.VectorStoreClient", _Fake)
        result = self._invoke("ingest", str(doc))

        assert result.exit_code == 0, result.output
        assert captured["paths"] == [doc]
        assert captured["wait"] == 0
        assert json.loads(result.output)["uploaded"] == ["vector-corpus/a.md"]

    def test_ingest_wait_flag_blocks_and_a_timeout_fails(self, tmp_path, monkeypatch):
        doc = tmp_path / "a.md"
        doc.write_text("x")
        captured = {}

        class _Fake:
            def __init__(self, **kwargs):
                pass

            def ingest(self, paths, wait_timeout_seconds):
                captured["wait"] = wait_timeout_seconds
                return {
                    "bucket": "b",
                    "uploaded": ["vector-corpus/a.md"],
                    "chunks_by_source": {"vector-corpus/a.md": 0},
                    "timed_out": True,
                }

        monkeypatch.setattr("cli.vector_store.VectorStoreClient", _Fake)
        result = self._invoke("ingest", str(doc), "--wait")

        assert captured["wait"] == 300
        assert result.exit_code == 1
