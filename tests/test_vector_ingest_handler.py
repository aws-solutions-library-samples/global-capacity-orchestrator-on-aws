# Tests for lambda/vector-ingest/handler.py — the S3-triggered write half
# of the opt-in vector store. Loaded via load_lambda_module so the module
# name cannot collide with other Lambdas' handlers; the three lazily built
# boto3 clients are replaced by presetting the module globals, so no test
# touches the network.

import hashlib
import io
import json

import pytest

from tests._lambda_imports import load_lambda_module

_TABLE = "gco-vector-store"
_MODEL = "amazon.titan-embed-text-v2:0"
_DIMS = 4
_PREFIX = "vector-corpus/"


class _FakeS3:
    def __init__(self, objects):
        self.objects = objects
        self.calls = []

    def get_object(self, Bucket, Key):
        self.calls.append((Bucket, Key))
        return {"Body": io.BytesIO(self.objects[Key])}


class _FakeDynamo:
    def __init__(self):
        self.items = []

    def put_item(self, TableName, Item):
        self.items.append((TableName, Item))


class _FakeBedrock:
    """Deterministic embedder: vector derived from the text's digest."""

    def __init__(self, dims=_DIMS, fail_marker=None):
        self.dims = dims
        self.fail_marker = fail_marker
        self.bodies = []

    def invoke_model(self, modelId, body, contentType, accept):
        self.bodies.append(json.loads(body))
        text = self.bodies[-1]["inputText"]
        if self.fail_marker and self.fail_marker in text:
            raise RuntimeError(f"synthetic embedding failure for {self.fail_marker!r}")
        digest = hashlib.sha256(text.encode()).digest()
        vector = [digest[i] / 255.0 for i in range(self.dims)]
        return {"body": io.BytesIO(json.dumps({"embedding": vector}).encode())}


@pytest.fixture()
def handler(monkeypatch):
    module = load_lambda_module("vector-ingest")
    monkeypatch.setenv("VECTOR_STORE_TABLE_NAME", _TABLE)
    monkeypatch.setenv("EMBEDDING_MODEL_ID", _MODEL)
    monkeypatch.setenv("EMBEDDING_DIMENSIONS", str(_DIMS))
    monkeypatch.setenv("CORPUS_PREFIX", _PREFIX)
    return module


def _wire(handler, *, objects, bedrock=None):
    """Preset the module's client globals; returns (s3, dynamo, bedrock)."""
    fakes = (_FakeS3(objects), _FakeDynamo(), bedrock or _FakeBedrock())
    handler._s3_client, handler._dynamodb_client, handler._bedrock_client = fakes
    return fakes


def _event(*keys, bucket="shared-bucket"):
    return {
        "Records": [{"s3": {"bucket": {"name": bucket}, "object": {"key": key}}} for key in keys]
    }


class TestChunker:
    def test_packs_paragraphs_up_to_the_ceiling(self, handler):
        text = "para one\n\npara two\n\npara three"
        assert handler.chunk_text(text, max_chars=25) == [
            "para one\n\npara two",
            "para three",
        ]

    def test_hard_splits_an_oversized_paragraph(self, handler):
        text = "a" * 4500
        chunks = handler.chunk_text(text)
        assert [len(c) for c in chunks] == [2000, 2000, 500]

    def test_is_deterministic_and_normalizes_crlf(self, handler):
        text = "one\r\n\r\ntwo"
        assert handler.chunk_text(text) == handler.chunk_text(text) == ["one\n\ntwo"]

    def test_empty_and_whitespace_only_input_yields_nothing(self, handler):
        assert handler.chunk_text("") == []
        assert handler.chunk_text("  \n\n   \n\n") == []

    def test_title_from_leading_markdown_heading_only(self, handler):
        assert handler._first_markdown_title("## Corpus Guide\n\nBody") == "Corpus Guide"
        assert handler._first_markdown_title("\n\n# Late Start\ntext") == "Late Start"
        assert handler._first_markdown_title("prose first\n# not a title") is None
        assert handler._first_markdown_title("#\nempty heading") is None


class TestJsonlPath:
    def test_parses_records_and_skips_blank_lines(self, handler):
        text = '{"text": "alpha"}\n\n{"text": "beta", "title": " T "}\n'
        records = handler._records_from_jsonl_object("k.jsonl", text)
        assert records == [
            {"text": "alpha", "chunk_index": 0},
            {"text": "beta", "chunk_index": 1, "title": "T"},
        ]

    @pytest.mark.parametrize(
        ("line", "message"),
        [
            ("{not json", "not valid JSON"),
            ('["array"]', "must be a JSON object"),
            ('{"title": "no text"}', "non-empty string 'text'"),
            ('{"text": "   "}', "non-empty string 'text'"),
            ('{"text": "' + "x" * 2001 + '"}', "exceeds 2000 characters"),
        ],
    )
    def test_malformed_line_fails_the_object_with_line_number(self, handler, line, message):
        with pytest.raises(ValueError, match="line 2") as exc_info:
            handler._records_from_jsonl_object("k.jsonl", '{"text": "ok"}\n' + line)
        assert message in str(exc_info.value)


class TestEmbeddingContract:
    def test_v2_family_gets_the_dimensions_key(self, handler):
        body = json.loads(handler._embedding_request_body("hi", _MODEL, _DIMS))
        assert body == {"inputText": "hi", "dimensions": _DIMS}

    def test_other_families_get_the_bare_body(self, handler):
        body = json.loads(
            handler._embedding_request_body("hi", "amazon.titan-embed-text-v1", _DIMS)
        )
        assert body == {"inputText": "hi"}

    def test_width_mismatch_is_a_hard_error_with_remediation(self, handler):
        handler._bedrock_client = _FakeBedrock(dims=3)
        with pytest.raises(ValueError, match="does not match the configured index width"):
            handler.embed_chunk("hello", _MODEL, _DIMS)

    def test_vectorless_response_is_a_hard_error(self, handler):
        class _NoVector:
            def invoke_model(self, **_):
                return {"body": io.BytesIO(b'{"unexpected": true}')}

        handler._bedrock_client = _NoVector()
        with pytest.raises(ValueError, match="carried no vector"):
            handler.embed_chunk("hello", _MODEL, _DIMS)


class TestLambdaHandler:
    def test_ingests_a_markdown_object_with_full_provenance(self, handler):
        key = f"{_PREFIX}guides/intro doc.md"
        content = "# Intro Guide\n\nFirst paragraph.\n\nSecond paragraph."
        _, dynamo, bedrock = _wire(handler, objects={key: content.encode()})

        # S3 event keys arrive URL-encoded: '+' for spaces.
        summary = handler.lambda_handler(_event("vector-corpus/guides/intro+doc.md"), context=None)

        assert summary["ingested_objects"] == 1
        assert summary["ingested_chunks"] == 1
        assert summary["failures"] == []
        ((table, item),) = dynamo.items
        assert table == _TABLE
        expected_digest = hashlib.sha256(key.encode()).hexdigest()[:16]
        assert item["doc_id"] == {"S": f"{expected_digest}#0000"}
        assert item["source"] == {"S": key}
        assert item["chunk_index"] == {"N": "0"}
        assert item["title"] == {"S": "Intro Guide"}
        assert item["embedding_model_id"] == {"S": _MODEL}
        assert item["content_sha256"] == {"S": hashlib.sha256(content.encode()).hexdigest()}
        assert "ingested_at" in item
        vector = item["embedding"]["L"]
        assert len(vector) == _DIMS
        assert all(set(component) == {"N"} for component in vector)
        # The Titan contract carried the configured width for the V2 family.
        assert bedrock.bodies[0]["dimensions"] == _DIMS

    def test_multi_chunk_objects_get_sequential_padded_doc_ids(self, handler):
        key = f"{_PREFIX}big.txt"
        content = ("y" * 1500 + "\n\n") * 3
        _, dynamo, _ = _wire(handler, objects={key: content.encode()})

        summary = handler.lambda_handler(_event(key), context=None)

        assert summary["ingested_chunks"] == 3
        suffixes = [item["doc_id"]["S"].split("#")[1] for _, item in dynamo.items]
        assert suffixes == ["0000", "0001", "0002"]
        indexes = [item["chunk_index"]["N"] for _, item in dynamo.items]
        assert indexes == ["0", "1", "2"]

    def test_jsonl_objects_use_the_prechunked_path(self, handler):
        key = f"{_PREFIX}records.jsonl"
        content = '{"text": "alpha", "title": "A"}\n{"text": "beta"}\n'
        _, dynamo, _ = _wire(handler, objects={key: content.encode()})

        summary = handler.lambda_handler(_event(key), context=None)

        assert summary["ingested_chunks"] == 2
        first_item = dynamo.items[0][1]
        second_item = dynamo.items[1][1]
        assert first_item["title"] == {"S": "A"}
        assert "title" not in second_item

    def test_prefix_guard_skips_outside_objects_without_reads(self, handler):
        s3, dynamo, _ = _wire(handler, objects={})

        summary = handler.lambda_handler(_event("elsewhere/file.md"), context=None)

        assert summary["ingested_objects"] == 0
        assert summary["skipped"] == [
            {"key": "elsewhere/file.md", "status": "skipped_outside_prefix", "chunks": 0}
        ]
        assert s3.calls == []
        assert dynamo.items == []

    def test_folder_markers_and_unsupported_suffixes_are_skipped(self, handler):
        s3, dynamo, _ = _wire(handler, objects={})

        summary = handler.lambda_handler(
            _event(f"{_PREFIX}subdir/", f"{_PREFIX}image.png"), context=None
        )

        statuses = [entry["status"] for entry in summary["skipped"]]
        assert statuses == ["skipped_folder_marker", "skipped_unsupported_suffix"]
        assert s3.calls == []
        assert dynamo.items == []

    def test_per_object_isolation_processes_the_batch_then_raises(self, handler):
        good_key = f"{_PREFIX}good.txt"
        bad_key = f"{_PREFIX}bad.txt"
        _, dynamo, _ = _wire(
            handler,
            objects={good_key: b"fine text", bad_key: b"poison text"},
            bedrock=_FakeBedrock(fail_marker="poison"),
        )

        with pytest.raises(RuntimeError, match="1 object"):
            handler.lambda_handler(_event(bad_key, good_key), context=None)

        # The failure did not block the second object.
        ((_, item),) = dynamo.items
        assert item["source"] == {"S": good_key}

    def test_undecodable_object_fails_that_object_only(self, handler):
        key = f"{_PREFIX}binary.md"
        _, dynamo, _ = _wire(handler, objects={key: b"\xff\xfe\x01"})

        with pytest.raises(RuntimeError, match="binary.md"):
            handler.lambda_handler(_event(key), context=None)
        assert dynamo.items == []

    def test_malformed_record_counts_as_a_failure(self, handler):
        _wire(handler, objects={})

        with pytest.raises(RuntimeError, match="1 object"):
            handler.lambda_handler({"Records": [{"s3": {"bucket": {}}}]}, context=None)

    def test_empty_document_reports_empty_not_failure(self, handler):
        key = f"{_PREFIX}empty.txt"
        _, dynamo, _ = _wire(handler, objects={key: b"   \n\n  "})

        summary = handler.lambda_handler(_event(key), context=None)

        assert summary["ingested_objects"] == 0
        assert summary["failures"] == []
        assert dynamo.items == []

    def test_missing_environment_fails_closed(self, handler, monkeypatch):
        monkeypatch.delenv("CORPUS_PREFIX")
        with pytest.raises(RuntimeError, match="CORPUS_PREFIX"):
            handler.lambda_handler(_event(f"{_PREFIX}a.md"), context=None)
