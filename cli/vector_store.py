"""Operator client for the GCO vector store (``gco vector``).

:class:`VectorStoreClient` is the CLI-side runtime for the
``{project}-vector-store`` DynamoDB global table provisioned by
``gco/stacks/global_stack.py`` when ``vector_store.enabled`` is set: an
S3-ingested, embedded document corpus searchable by similarity through
the ``corpus-embedding-index`` vector index, replicated to every
deployment region.

Three operations:

* :meth:`VectorStoreClient.ingest` — upload documents to the
  cluster-shared bucket's corpus prefix; the S3-triggered
  ``lambda/vector-ingest`` handler chunks, embeds, and writes them.
  Optionally wait until every uploaded document is searchable.
* :meth:`VectorStoreClient.search` — embed a query with the corpus's own
  model and ``SearchVectors`` the index, optionally against a specific
  replica region and/or filtered to one source document.
* :meth:`VectorStoreClient.status` — table/replica/index state plus the
  resolved names, for "is it ready yet?" (the index takes several
  minutes to reach ACTIVE after first deploy).

Resolution conventions, copied from their precedents:

* **Region** follows the store convention documented in
  ``gco/services/template_store.py``: ``DYNAMODB_REGION`` →
  ``GLOBAL_REGION`` → ``AWS_REGION``, else the SDK default chain. A
  ``query_region`` override retargets ONLY the DynamoDB data client —
  that is how ``gco vector search --region`` reads a specific replica —
  while SSM discovery and embedding stay on the default chain (the
  parameters live in the global region; the query vector can be
  produced anywhere).
* **Names** resolve lazily from SSM (``/{project}/vector-store-table-name``,
  ``/{project}/vector-store-index-name``, and the cluster-shared bucket
  metadata under ``/{project}/cluster-shared-bucket/``) on first use and
  are cached on the instance, the same shape as
  ``mcp.mission.memory.MissionMemoryStore``.

Request-shape gotchas, live-verified in the Phase 2 spike:
``SearchVectors``' ``SearchVector`` parameter is a plain list of
``{"N": "..."}`` attribute values (the ``{"L": ...}`` wrapper is a
write-side shape), and the response carries hits under ``SearchResults``
as ``{"Item": ..., "Score": float}`` — a lower COSINE score is closer.

Failure contract: infrastructure that is absent or not yet queryable —
table/index missing, the index still building after first deploy
(``SearchVectors`` answers ValidationException for several minutes), SSM
parameters never published because the feature is disabled, missing
credentials, unreachable endpoint — raises
:class:`VectorStoreUnavailableError`; everything else raises
:class:`VectorStoreError`. The CLI surfaces both messages.

The defaults below mirror the ``vector_store`` block in ``cdk.json`` —
the block drives the *deployed index and ingest pipeline*, these drive
the *query vectors sent to it*, and ``tests/test_vector_cli.py`` asserts
the two stay in agreement. ``dimensions`` is a one-way door: immutable
after index creation, and query vectors must come from the same model at
the same width or similarity scores are meaningless.
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

__all__ = [
    "DEFAULT_CORPUS_PREFIX",
    "DEFAULT_DIMENSIONS",
    "DEFAULT_EMBEDDING_MODEL_ID",
    "DEFAULT_TOP_K",
    "VectorStoreClient",
    "VectorStoreError",
    "VectorStoreUnavailableError",
]

#: Defaults mirroring the ``vector_store`` block in ``cdk.json``.
DEFAULT_DIMENSIONS = 1024
DEFAULT_EMBEDDING_MODEL_ID = "amazon.titan-embed-text-v2:0"
DEFAULT_CORPUS_PREFIX = "vector-corpus/"
DEFAULT_TOP_K = 5

#: Model-id substrings whose Titan request body accepts the V2-only
#: ``dimensions`` key — the identical contract the ingest Lambda applies
#: (``lambda/vector-ingest/handler.py``), pinned against it by
#: ``tests/test_vector_cli.py`` so query and corpus vectors can never
#: diverge in request shape.
DIMENSIONS_CAPABLE_MODEL_MARKERS = ("titan-embed-text-v2",)

_TABLE_NAME_PARAM_SUFFIX = "vector-store-table-name"
_INDEX_NAME_PARAM_SUFFIX = "vector-store-index-name"
_BUCKET_NAME_PARAM_SUFFIX = "cluster-shared-bucket/name"
_BUCKET_REGION_PARAM_SUFFIX = "cluster-shared-bucket/region"

#: Suffixes the ingest pipeline understands; everything else is skipped
#: server-side, so refusing the upload client-side is kinder.
_INGESTIBLE_SUFFIXES = (".txt", ".md", ".jsonl")

_UNAVAILABLE_HINT = (
    "the vector store is unavailable — the table or index may not be "
    "provisioned (deploy with vector_store.enabled: true), or the vector "
    "index may still be building after the first deploy (several minutes)"
)

#: Region resolution order shared with the sibling DynamoDB stores.
_REGION_ENV_ORDER = ("DYNAMODB_REGION", "GLOBAL_REGION", "AWS_REGION")


class VectorStoreError(RuntimeError):
    """A vector-store operation failed."""


class VectorStoreUnavailableError(VectorStoreError):
    """The vector-store infrastructure is absent or not yet queryable.

    Raised for the degradable cases: table/index not provisioned, SSM
    name parameters never published, the vector index still building
    after creation, missing credentials, or an unreachable endpoint —
    so an operator can tell "no matches" from "not there".
    """


def _resolve_region() -> str | None:
    """Return the first configured region env var, else ``None``."""
    for env_var in _REGION_ENV_ORDER:
        value = os.environ.get(env_var)
        if value:
            return value
    return None


def _number_attr(value: float) -> dict[str, str]:
    """Render one vector component as a DynamoDB number attribute value."""
    return {"N": repr(float(value))}


def _plain(value: Any) -> Any:
    """Collapse ``TypeDeserializer`` output into JSON-friendly primitives."""
    from decimal import Decimal

    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, list):
        return [_plain(entry) for entry in value]
    if isinstance(value, dict):
        return {key: _plain(entry) for key, entry in value.items()}
    return value


def _embedding_request_body(text: str, model_id: str, dimensions: int) -> str:
    """Build the Titan-contract request body for one query.

    Byte-identical to the ingest Lambda's builder: the ``dimensions``
    key rides only for model families known to accept it, so a V1-style
    corpus keeps working and a V2 corpus always pins the width.
    """
    body: dict[str, Any] = {"inputText": text}
    if any(marker in model_id for marker in DIMENSIONS_CAPABLE_MODEL_MARKERS):
        body["dimensions"] = dimensions
    return json.dumps(body)


class VectorStoreClient:
    """Operator client for ingest, search, and status.

    Constructor arguments exist for tests and for callers that already
    know the deployed names; with no arguments every name resolves
    lazily from SSM on first use, so constructing a client on a host
    without AWS credentials is free.

    Args:
        table_name: Table name override; ``None`` resolves from SSM.
        index_name: Index name override; ``None`` resolves from SSM.
        bucket_name: Corpus bucket override; ``None`` resolves from SSM.
        query_region: Region whose replica the DynamoDB data client
            reads (``gco vector search --region``). ``None`` follows the
            default chain. SSM and embedding never follow this override.
        embedding_model_id: Embedding model override. Must match the
            model the corpus was ingested with.
        dimensions: Requested embedding width. Must match the deployed
            index width (one-way door).
        corpus_prefix: S3 key prefix the ingest pipeline watches.
    """

    def __init__(
        self,
        table_name: str | None = None,
        index_name: str | None = None,
        *,
        bucket_name: str | None = None,
        query_region: str | None = None,
        embedding_model_id: str = DEFAULT_EMBEDDING_MODEL_ID,
        dimensions: int = DEFAULT_DIMENSIONS,
        corpus_prefix: str = DEFAULT_CORPUS_PREFIX,
    ) -> None:
        self._table_name = table_name
        self._index_name = index_name
        self._bucket_name = bucket_name
        self._bucket_region: str | None = None
        self._query_region = query_region
        self._embedding_model_id = embedding_model_id
        self._dimensions = int(dimensions)
        self._corpus_prefix = corpus_prefix
        self._dynamodb_client: Any = None
        self._bedrock_client: Any = None
        self._s3_client: Any = None

    # ------------------------------------------------------------------ #
    # internals
    # ------------------------------------------------------------------ #

    def _resolve_ssm_name(self, suffix: str) -> str:
        """Fetch ``/{project}/{suffix}`` from SSM, mapping absence to unavailable."""
        from botocore.exceptions import BotoCoreError, ClientError

        from gco.services.aws_ssm import get_ssm_parameter

        project_name = os.environ.get("GCO_PROJECT_NAME", "gco")
        param_name = f"/{project_name}/{suffix}"
        try:
            return get_ssm_parameter(param_name, region=_resolve_region())
        except ClientError as err:
            code = err.response.get("Error", {}).get("Code")
            if code == "ParameterNotFound":
                raise VectorStoreUnavailableError(
                    f"SSM parameter {param_name} not found — {_UNAVAILABLE_HINT}"
                ) from err
            raise VectorStoreError(f"SSM lookup for {param_name} failed: {err}") from err
        except BotoCoreError as err:
            raise VectorStoreUnavailableError(
                f"SSM unreachable resolving {param_name} — {_UNAVAILABLE_HINT}"
            ) from err

    def _resolve_table_name(self) -> str:
        if self._table_name is None:
            self._table_name = self._resolve_ssm_name(_TABLE_NAME_PARAM_SUFFIX)
        return self._table_name

    def _resolve_index_name(self) -> str:
        if self._index_name is None:
            self._index_name = self._resolve_ssm_name(_INDEX_NAME_PARAM_SUFFIX)
        return self._index_name

    def _resolve_bucket(self) -> tuple[str, str | None]:
        """Return ``(bucket_name, bucket_region)`` for corpus uploads."""
        if self._bucket_name is None:
            self._bucket_name = self._resolve_ssm_name(_BUCKET_NAME_PARAM_SUFFIX)
            self._bucket_region = self._resolve_ssm_name(_BUCKET_REGION_PARAM_SUFFIX)
        return self._bucket_name, self._bucket_region

    def _get_dynamodb_client(self) -> Any:
        if self._dynamodb_client is None:
            import boto3

            self._dynamodb_client = boto3.client(
                "dynamodb", region_name=self._query_region or _resolve_region()
            )
        return self._dynamodb_client

    def _get_bedrock_client(self) -> Any:
        if self._bedrock_client is None:
            import boto3

            self._bedrock_client = boto3.client("bedrock-runtime", region_name=_resolve_region())
        return self._bedrock_client

    def _get_s3_client(self, bucket_region: str | None) -> Any:
        if self._s3_client is None:
            import boto3

            self._s3_client = boto3.client("s3", region_name=bucket_region or _resolve_region())
        return self._s3_client

    def _embed(self, text: str) -> list[float]:
        """Embed ``text`` at the configured width and guard the one-way door."""
        from botocore.exceptions import (
            BotoCoreError,
            ClientError,
            NoCredentialsError,
            PartialCredentialsError,
        )

        if not text.strip():
            raise VectorStoreError("query text must be non-empty")

        try:
            response = self._get_bedrock_client().invoke_model(
                modelId=self._embedding_model_id,
                body=_embedding_request_body(text, self._embedding_model_id, self._dimensions),
                contentType="application/json",
                accept="application/json",
            )
        except (NoCredentialsError, PartialCredentialsError) as err:
            raise VectorStoreUnavailableError(f"no AWS credentials — {_UNAVAILABLE_HINT}") from err
        except ClientError as err:
            code = err.response.get("Error", {}).get("Code") or "ClientError"
            raise VectorStoreError(f"query embedding failed (bedrock {code}): {err}") from err
        except BotoCoreError as err:
            raise VectorStoreUnavailableError(f"Bedrock unreachable — {_UNAVAILABLE_HINT}") from err

        payload = json.loads(response["body"].read())
        vector = payload.get("embedding") if isinstance(payload, dict) else None
        if not isinstance(vector, list) or not vector:
            raise VectorStoreError(
                f"embedding response from {self._embedding_model_id!r} carried no vector"
            )
        if len(vector) != self._dimensions:
            raise VectorStoreError(
                f"embedding width {len(vector)} does not match the configured index "
                f"width {self._dimensions}; the model {self._embedding_model_id!r} "
                "ignored the requested dimensions — align vector_store.dimensions "
                "with the model's output width (both are one-way doors on the "
                "deployed index)"
            )
        return [float(value) for value in vector]

    # ------------------------------------------------------------------ #
    # operations
    # ------------------------------------------------------------------ #

    def search(
        self,
        query: str,
        top_k: int = DEFAULT_TOP_K,
        source: str | None = None,
    ) -> list[dict[str, Any]]:
        """Return the corpus chunks most similar to ``query``.

        Args:
            query: Query text; embedded with the same model and width
                the index was built for.
            top_k: Number of results to request.
            source: When set, an inline equality filter on the chunk's
                ``source`` (the full S3 object key) — the one attribute
                the index declares as an ``INLINE_FILTER``.

        Returns:
            A list of plain dicts — the index's ``INCLUDE`` projection
            (``text``, ``source``, ``chunk_index``, ``title``,
            ``embedding_model_id``) plus ``doc_id`` and a float
            ``score`` (lower is closer under COSINE).

        Raises:
            VectorStoreUnavailableError: Table/index absent, index still
                building, no credentials, or endpoint unreachable.
            VectorStoreError: ``top_k`` < 1 or any other failure.
        """
        from botocore.exceptions import (
            BotoCoreError,
            ClientError,
            NoCredentialsError,
            PartialCredentialsError,
        )

        if int(top_k) < 1:
            raise VectorStoreError(f"top_k must be a positive integer, got {top_k!r}")

        # Resolve names before embedding: a deployment without the feature
        # fails fast on the SSM lookup instead of paying a Bedrock call.
        table_name = self._resolve_table_name()
        index_name = self._resolve_index_name()
        vector = self._embed(query)

        request: dict[str, Any] = {
            "TableName": table_name,
            "IndexName": index_name,
            "SearchVector": [_number_attr(value) for value in vector],
            "TopK": int(top_k),
        }
        if source is not None:
            request["SearchConditionExpression"] = "source = :source"
            request["ExpressionAttributeValues"] = {":source": {"S": str(source)}}

        try:
            response = self._get_dynamodb_client().search_vectors(**request)
        except (NoCredentialsError, PartialCredentialsError) as err:
            raise VectorStoreUnavailableError(f"no AWS credentials — {_UNAVAILABLE_HINT}") from err
        except ClientError as err:
            code = err.response.get("Error", {}).get("Code")
            if code in ("ResourceNotFoundException", "ValidationException"):
                # ValidationException is what SearchVectors answers while
                # the index is still building after creation.
                raise VectorStoreUnavailableError(
                    f"SearchVectors failed ({code}) — {_UNAVAILABLE_HINT}: {err}"
                ) from err
            raise VectorStoreError(f"vector-store search failed: {err}") from err
        except BotoCoreError as err:
            raise VectorStoreUnavailableError(
                f"DynamoDB unreachable — {_UNAVAILABLE_HINT}"
            ) from err

        from boto3.dynamodb.types import TypeDeserializer

        deserializer = TypeDeserializer()
        results: list[dict[str, Any]] = []
        for entry in response.get("SearchResults", []):
            item = {
                key: _plain(deserializer.deserialize(value))
                for key, value in (entry.get("Item") or {}).items()
            }
            # Defensive: the INCLUDE projection excludes the vector, but a
            # recreated index with a different projection must not flood
            # terminal output with a thousand floats.
            item.pop("embedding", None)
            score = entry.get("Score")
            if score is not None:
                item["score"] = float(score)
            results.append(item)
        return results

    def status(self) -> dict[str, Any]:
        """Return table, replica, and index state for the store.

        The index takes several minutes to reach ACTIVE after the first
        deploy (queries answer ValidationException until then), so this
        is the "is it ready yet?" surface.

        Raises:
            VectorStoreUnavailableError: SSM names absent (feature not
                provisioned), the table missing, no credentials, or the
                endpoint unreachable.
            VectorStoreError: Any other failure.
        """
        from botocore.exceptions import (
            BotoCoreError,
            ClientError,
            NoCredentialsError,
            PartialCredentialsError,
        )

        table_name = self._resolve_table_name()
        index_name = self._resolve_index_name()
        try:
            table = self._get_dynamodb_client().describe_table(TableName=table_name)["Table"]
        except (NoCredentialsError, PartialCredentialsError) as err:
            raise VectorStoreUnavailableError(f"no AWS credentials — {_UNAVAILABLE_HINT}") from err
        except ClientError as err:
            code = err.response.get("Error", {}).get("Code")
            if code == "ResourceNotFoundException":
                raise VectorStoreUnavailableError(
                    f"table {table_name} not found in this region — {_UNAVAILABLE_HINT}"
                ) from err
            raise VectorStoreError(f"DescribeTable failed: {err}") from err
        except BotoCoreError as err:
            raise VectorStoreUnavailableError(
                f"DynamoDB unreachable — {_UNAVAILABLE_HINT}"
            ) from err

        # Vector indexes surface under a dedicated describe key; read it
        # defensively (any key naming "Vector") the way the Phase 2 spike
        # did, so a rename in the evolving API degrades to NOT_VISIBLE
        # instead of a KeyError.
        index_status = "NOT_VISIBLE"
        for key, value in table.items():
            if "Vector" in key and isinstance(value, list):
                for index in value:
                    if isinstance(index, dict) and index.get("IndexName") == index_name:
                        index_status = str(index.get("IndexStatus", "UNKNOWN"))
        return {
            "table_name": table_name,
            "index_name": index_name,
            "region": self._query_region or _resolve_region() or "sdk-default",
            "table_status": table.get("TableStatus"),
            "item_count": int(table.get("ItemCount", 0)),
            "index_status": index_status,
            "replicas": [
                {
                    "region": replica.get("RegionName"),
                    "status": replica.get("ReplicaStatus"),
                }
                for replica in table.get("Replicas", [])
            ],
        }

    def ingest(
        self,
        paths: list[Path],
        *,
        wait_timeout_seconds: int = 0,
    ) -> dict[str, Any]:
        """Upload documents to the corpus prefix and optionally wait.

        Uploading IS ingestion: the S3 notification invokes the ingest
        Lambda, which chunks, embeds, and writes the items. With
        ``wait_timeout_seconds`` > 0, polls the table until every
        uploaded document has at least one chunk item (matching on the
        chunk's ``source`` key) or the timeout elapses.

        Args:
            paths: Files to upload. Every path must exist and carry an
                ingestible suffix (.txt, .md, .jsonl).
            wait_timeout_seconds: 0 returns right after the uploads.

        Returns:
            A summary dict: the bucket, per-file uploaded keys, and —
            when waiting — per-source chunk counts and whether the wait
            timed out.

        Raises:
            VectorStoreUnavailableError: Bucket/table SSM names absent,
                no credentials, or endpoint unreachable.
            VectorStoreError: A path is missing or not ingestible, or
                the upload fails.
        """
        from botocore.exceptions import BotoCoreError, ClientError

        if not paths:
            raise VectorStoreError("nothing to ingest: no files given")
        for path in paths:
            if not path.is_file():
                raise VectorStoreError(f"not a file: {path}")
            if path.suffix.lower() not in _INGESTIBLE_SUFFIXES:
                raise VectorStoreError(
                    f"{path.name}: unsupported suffix {path.suffix!r} — the ingest "
                    f"pipeline reads {', '.join(_INGESTIBLE_SUFFIXES)}"
                )

        bucket_name, bucket_region = self._resolve_bucket()
        client = self._get_s3_client(bucket_region)
        uploaded: list[str] = []
        for path in paths:
            key = f"{self._corpus_prefix}{path.name}"
            try:
                client.put_object(Bucket=bucket_name, Key=key, Body=path.read_bytes())
            except (ClientError, BotoCoreError) as err:
                raise VectorStoreError(f"upload of {path.name} failed: {err}") from err
            uploaded.append(key)

        summary: dict[str, Any] = {"bucket": bucket_name, "uploaded": uploaded}
        if wait_timeout_seconds > 0:
            summary.update(self._wait_for_sources(uploaded, wait_timeout_seconds))
        return summary

    def _wait_for_sources(self, keys: list[str], timeout_seconds: int) -> dict[str, Any]:
        """Poll until every source key has at least one chunk item.

        A filtered ``Scan`` per poll is deliberate: there is no
        by-source key schema, corpora are document-scale (not
        row-scale), and this runs only in the interactive --wait path.
        """
        from botocore.exceptions import BotoCoreError, ClientError

        table_name = self._resolve_table_name()
        client = self._get_dynamodb_client()
        deadline = time.monotonic() + timeout_seconds
        counts: dict[str, int] = dict.fromkeys(keys, 0)
        while True:
            for key in keys:
                try:
                    counts[key] = self._count_source_chunks(client, table_name, key)
                except ClientError as err:
                    code = err.response.get("Error", {}).get("Code")
                    if code == "ResourceNotFoundException":
                        raise VectorStoreUnavailableError(
                            f"table {table_name} not found — {_UNAVAILABLE_HINT}"
                        ) from err
                    raise VectorStoreError(f"ingest wait failed: {err}") from err
                except BotoCoreError as err:
                    raise VectorStoreUnavailableError(
                        f"DynamoDB unreachable — {_UNAVAILABLE_HINT}"
                    ) from err
            if all(count > 0 for count in counts.values()):
                return {"chunks_by_source": counts, "timed_out": False}
            if time.monotonic() >= deadline:
                return {"chunks_by_source": counts, "timed_out": True}
            time.sleep(3)

    @staticmethod
    def _count_source_chunks(client: Any, table_name: str, source_key: str) -> int:
        """Count chunk items for one source key with a paginated filtered Scan."""
        count = 0
        start_key: dict[str, Any] | None = None
        while True:
            request: dict[str, Any] = {
                "TableName": table_name,
                "Select": "COUNT",
                "FilterExpression": "#source = :source",
                "ExpressionAttributeNames": {"#source": "source"},
                "ExpressionAttributeValues": {":source": {"S": source_key}},
            }
            if start_key:
                request["ExclusiveStartKey"] = start_key
            response = client.scan(**request)
            count += int(response.get("Count", 0))
            start_key = response.get("LastEvaluatedKey")
            if not start_key:
                return count


def demo_corpus_paths() -> list[Path]:
    """Return the checkout's ``docs/*.md`` as a self-contained demo corpus.

    ``gco vector ingest --demo`` seeds the store with GCO's own feature
    documentation — always present in a source checkout, meaningful to
    search ("how does capacity history work?"), and free of licensing
    questions. Installed (pip/uvx) distributions do not carry ``docs/``,
    so the demo requires a checkout.
    """
    docs_dir = Path(__file__).resolve().parent.parent / "docs"
    markers = (docs_dir.parent / "app.py", docs_dir.parent / "pyproject.toml")
    if not docs_dir.is_dir() or not all(marker.is_file() for marker in markers):
        raise VectorStoreError(
            "the demo corpus is the checkout's docs/*.md — run from a GCO "
            "source checkout, or pass explicit files to ingest instead"
        )
    paths = sorted(docs_dir.glob("*.md"))
    if not paths:
        raise VectorStoreError(f"no *.md files found under {docs_dir}")
    return paths
