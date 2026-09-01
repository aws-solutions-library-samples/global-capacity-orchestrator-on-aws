"""S3-triggered ingest for the GCO vector store.

Objects dropped under the configured corpus prefix of the
Cluster_Shared_Bucket are chunked, embedded with the configured Bedrock
text-embedding model, and written to the ``{project}-vector-store``
DynamoDB global table in this (global) region; global-table replication
fans the items out to every replica so workloads query locally.

Wire conventions deliberately mirror the mission-memory runtime
(``gco_mcp/mission/memory.py``): the embedding request body follows the
Amazon Titan Text Embeddings contract (``inputText`` plus the V2-only
``dimensions`` key, omitted for model families that reject it), and the
vector attribute is a DynamoDB number list (``{"L": [{"N": ...}]}``)
written through the low-level client.

Determinism and idempotency: chunking is a pure function of the object
bytes, and ``doc_id`` is ``sha256(key)[:16]#<chunk_index:04d>`` — so
re-delivering an event (S3 retries at-least-once) or re-uploading an
object overwrites the same items instead of duplicating them. Two
consequences are documented feature limits rather than handled here:
deleting an S3 object does not delete its items, and a shrinking object
leaves its tail chunks behind until the corpus is re-ingested.

Failure posture: objects are isolated — one undecodable or oversized
object never blocks the rest of the batch — but any per-object failure
re-raises after the batch so the async-invoke retry/DLQ machinery
engages (succeeded objects are idempotent on the retry). The summary of
every invocation is logged as one JSON line for operability.

Only boto3/botocore and the standard library are used, matching every
other GCO Lambda (no bundling step).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import urllib.parse
from datetime import UTC, datetime
from typing import Any

import boto3

# <pyflowchart-code-diagram> BEGIN - auto-inserted, do not edit
# Generated at (UTC): 2026-09-01T13:22:56Z
# Generated from Git commit: ed395032d46063f44b638deb85ae2a6dbf98e7f4
# Flowchart(s) generated from this file:
#   * ``lambda_handler`` -> ``diagrams/code_diagrams/lambda/vector-ingest/handler.lambda_handler.html``
#     (PNG: ``diagrams/code_diagrams/lambda/vector-ingest/handler.lambda_handler.png``)
# Regenerate with ``SOURCE_DATE_EPOCH=<unix-seconds> GCO_DIAGRAM_SOURCE_COMMIT=<40-char-sha> python diagrams/generate.py --code-only``.
# <pyflowchart-code-diagram> END


logger = logging.getLogger()
logger.setLevel(logging.INFO)

#: Chunk-size ceiling, in characters. Paragraphs are packed greedily up
#: to this bound; a single longer paragraph is hard-split at exactly this
#: width. ~2000 chars keeps each chunk comfortably inside Titan's 8k-token
#: input window while staying large enough to carry a coherent passage.
MAX_CHUNK_CHARS = 2000

#: Object-key suffixes routed to the plain-text paragraph chunker.
TEXT_SUFFIXES = (".txt", ".md")

#: Object-key suffix routed to the pre-chunked JSON-lines path.
JSONL_SUFFIX = ".jsonl"

#: Model-id substrings whose Titan request body accepts the V2-only
#: ``dimensions`` key. Anything else gets the bare ``inputText`` body —
#: V1-family models reject the key outright — and relies on the width
#: verification below to catch a model whose default width disagrees
#: with the deployed index.
DIMENSIONS_CAPABLE_MODEL_MARKERS = ("titan-embed-text-v2",)

_s3_client: Any = None
_dynamodb_client: Any = None
_bedrock_client: Any = None


def _get_s3_client() -> Any:
    global _s3_client
    if _s3_client is None:
        _s3_client = boto3.client("s3")
    return _s3_client


def _get_dynamodb_client() -> Any:
    global _dynamodb_client
    if _dynamodb_client is None:
        _dynamodb_client = boto3.client("dynamodb")
    return _dynamodb_client


def _get_bedrock_client() -> Any:
    global _bedrock_client
    if _bedrock_client is None:
        _bedrock_client = boto3.client("bedrock-runtime")
    return _bedrock_client


def _require_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"Required environment variable {name} is not set")
    return value


def _first_markdown_title(text: str) -> str | None:
    """Return the first ATX heading's text, if the document opens with one."""
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("#"):
            title = stripped.lstrip("#").strip()
            return title or None
        return None
    return None


def chunk_text(text: str, max_chars: int = MAX_CHUNK_CHARS) -> list[str]:
    """Split ``text`` into deterministic ~``max_chars`` paragraph packs.

    Paragraphs (blank-line separated) are packed greedily, joined by a
    blank line, without ever crossing ``max_chars``; a single paragraph
    longer than ``max_chars`` is hard-split at exactly ``max_chars``.
    Pure function of its inputs: identical bytes always produce the
    identical chunk list, which is what makes ``doc_id`` idempotent.
    """
    paragraphs = [p.strip() for p in text.replace("\r\n", "\n").split("\n\n")]
    paragraphs = [p for p in paragraphs if p]

    chunks: list[str] = []
    current = ""
    for paragraph in paragraphs:
        pieces = (
            [paragraph[i : i + max_chars] for i in range(0, len(paragraph), max_chars)]
            if len(paragraph) > max_chars
            else [paragraph]
        )
        for piece in pieces:
            if not current:
                current = piece
            elif len(current) + 2 + len(piece) <= max_chars:
                current = f"{current}\n\n{piece}"
            else:
                chunks.append(current)
                current = piece
    if current:
        chunks.append(current)
    return chunks


def _records_from_text_object(key: str, text: str) -> list[dict[str, Any]]:
    """Chunk a .txt/.md object into item-shaped records."""
    title = _first_markdown_title(text)
    records = []
    for index, chunk in enumerate(chunk_text(text)):
        record: dict[str, Any] = {"text": chunk, "chunk_index": index}
        if title:
            record["title"] = title
        records.append(record)
    return records


def _records_from_jsonl_object(key: str, text: str) -> list[dict[str, Any]]:
    """Parse a pre-chunked .jsonl object into item-shaped records.

    Each non-empty line must be a JSON object with a non-empty string
    ``text``; ``title`` is optional. A malformed line fails the whole
    object (isolation stays at object granularity so a partially
    ingested document never looks complete).
    """
    records: list[dict[str, Any]] = []
    for line_number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except ValueError as err:
            raise ValueError(f"{key}: line {line_number} is not valid JSON: {err}") from err
        if not isinstance(payload, dict):
            raise ValueError(f"{key}: line {line_number} must be a JSON object")
        text_value = payload.get("text")
        if not isinstance(text_value, str) or not text_value.strip():
            raise ValueError(f"{key}: line {line_number} needs a non-empty string 'text'")
        if len(text_value) > MAX_CHUNK_CHARS:
            raise ValueError(
                f"{key}: line {line_number} text exceeds {MAX_CHUNK_CHARS} characters; "
                "pre-chunked records must fit one chunk"
            )
        record: dict[str, Any] = {"text": text_value.strip(), "chunk_index": len(records)}
        title = payload.get("title")
        if isinstance(title, str) and title.strip():
            record["title"] = title.strip()
        records.append(record)
    return records


def _embedding_request_body(text: str, model_id: str, dimensions: int) -> str:
    """Build the Titan-contract request body for one chunk.

    The ``dimensions`` key is sent only to model families known to accept
    it (Titan Text Embeddings V2); V1-style models reject unknown keys,
    so they get the bare body and the width check below arbitrates.
    """
    body: dict[str, Any] = {"inputText": text}
    if any(marker in model_id for marker in DIMENSIONS_CAPABLE_MODEL_MARKERS):
        body["dimensions"] = dimensions
    return json.dumps(body)


def embed_chunk(text: str, model_id: str, dimensions: int) -> list[float]:
    """Embed one chunk and verify the vector width.

    A width mismatch is a hard error: the deployed index width is a
    one-way door, and a wrong-width vector would either be rejected by
    DynamoDB or (worse, with a misconfigured index) silently poison
    similarity results.
    """
    response = _get_bedrock_client().invoke_model(
        modelId=model_id,
        body=_embedding_request_body(text, model_id, dimensions),
        contentType="application/json",
        accept="application/json",
    )
    payload = json.loads(response["body"].read())
    vector = payload.get("embedding") if isinstance(payload, dict) else None
    if not isinstance(vector, list) or not vector:
        raise ValueError(f"embedding response from {model_id} carried no vector")
    if len(vector) != dimensions:
        raise ValueError(
            f"embedding width {len(vector)} from {model_id} does not match the "
            f"configured index width {dimensions}; check vector_store.dimensions "
            "and vector_store.embedding_model_id (changing either after index "
            "creation means re-creating the index and re-ingesting the corpus)"
        )
    return [float(value) for value in vector]


def _number_attr(value: float) -> dict[str, str]:
    """Render one vector component as a DynamoDB number attribute value."""
    return {"N": repr(float(value))}


def _put_chunk_item(
    table_name: str,
    *,
    key: str,
    record: dict[str, Any],
    vector: list[float],
    model_id: str,
    content_sha256: str,
    ingested_at: str,
) -> str:
    """Write one chunk item; returns its deterministic ``doc_id``."""
    key_digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]
    doc_id = f"{key_digest}#{int(record['chunk_index']):04d}"
    item: dict[str, Any] = {
        "doc_id": {"S": doc_id},
        "text": {"S": record["text"]},
        "source": {"S": key},
        "chunk_index": {"N": str(int(record["chunk_index"]))},
        "embedding": {"L": [_number_attr(value) for value in vector]},
        # Provenance: vectors are only comparable to vectors from the same
        # model, and the content hash makes re-ingest audits cheap.
        "embedding_model_id": {"S": model_id},
        "content_sha256": {"S": content_sha256},
        "ingested_at": {"S": ingested_at},
    }
    if record.get("title"):
        item["title"] = {"S": str(record["title"])}
    _get_dynamodb_client().put_item(TableName=table_name, Item=item)
    return doc_id


def _ingest_object(bucket: str, key: str) -> dict[str, Any]:
    """Fetch, chunk, embed, and store one S3 object."""
    table_name = _require_env("VECTOR_STORE_TABLE_NAME")
    model_id = _require_env("EMBEDDING_MODEL_ID")
    dimensions = int(_require_env("EMBEDDING_DIMENSIONS"))

    raw = _get_s3_client().get_object(Bucket=bucket, Key=key)["Body"].read()
    content_sha256 = hashlib.sha256(raw).hexdigest()
    text = raw.decode("utf-8")

    lowered = key.lower()
    if lowered.endswith(JSONL_SUFFIX):
        records = _records_from_jsonl_object(key, text)
    else:
        records = _records_from_text_object(key, text)
    if not records:
        return {"key": key, "status": "empty", "chunks": 0}

    ingested_at = datetime.now(UTC).isoformat()
    doc_ids = []
    for record in records:
        vector = embed_chunk(record["text"], model_id, dimensions)
        doc_ids.append(
            _put_chunk_item(
                table_name,
                key=key,
                record=record,
                vector=vector,
                model_id=model_id,
                content_sha256=content_sha256,
                ingested_at=ingested_at,
            )
        )
    return {"key": key, "status": "ingested", "chunks": len(doc_ids)}


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """Process one S3 notification event, object by object.

    Every record is attempted (per-object isolation); a summary line is
    logged either way; any failure re-raises afterwards so the async
    retry/DLQ machinery sees the invocation as failed. Retries are safe:
    ``doc_id`` is deterministic, so already-ingested objects overwrite
    in place.
    """
    corpus_prefix = _require_env("CORPUS_PREFIX")
    results: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []

    for record in event.get("Records", []):
        s3_info = record.get("s3") or {}
        bucket = (s3_info.get("bucket") or {}).get("name")
        raw_key = (s3_info.get("object") or {}).get("key")
        if not bucket or not raw_key:
            failures.append({"key": raw_key, "status": "malformed_record"})
            continue
        # Event keys arrive URL-encoded (spaces as '+', unicode escaped).
        key = urllib.parse.unquote_plus(raw_key)

        # Defense in depth: the bucket notification already filters on the
        # prefix, but the guard keeps a mis-wired notification from
        # ingesting arbitrary bucket contents.
        if not key.startswith(corpus_prefix):
            results.append({"key": key, "status": "skipped_outside_prefix", "chunks": 0})
            continue
        if key.endswith("/"):
            results.append({"key": key, "status": "skipped_folder_marker", "chunks": 0})
            continue
        lowered = key.lower()
        if not lowered.endswith(TEXT_SUFFIXES) and not lowered.endswith(JSONL_SUFFIX):
            results.append({"key": key, "status": "skipped_unsupported_suffix", "chunks": 0})
            continue

        try:
            results.append(_ingest_object(bucket, key))
        except Exception as err:  # noqa: BLE001 — per-object isolation, re-raised below
            logger.exception("vector-ingest failed for s3://%s/%s", bucket, key)
            failures.append({"key": key, "status": "failed", "error": str(err)})

    summary = {
        "message": "vector-ingest summary",
        "ingested_objects": sum(1 for r in results if r["status"] == "ingested"),
        "ingested_chunks": sum(r.get("chunks", 0) for r in results),
        "skipped": [r for r in results if r["status"].startswith("skipped")],
        "failures": failures,
    }
    logger.info(json.dumps(summary, default=str))

    if failures:
        raise RuntimeError(
            f"vector-ingest failed for {len(failures)} object(s): "
            + ", ".join(str(f["key"]) for f in failures)
        )
    return summary
