"""Mission-memory store over the DynamoDB vector index.

:class:`MissionMemoryStore` is the runtime client for the
``{project}-mission-memory`` table provisioned by
``gco/stacks/global_stack.py`` when ``mission_memory.enabled`` is set:
one memory item per completed Mission session, searchable by directive
similarity through the ``directive-embedding-index`` vector index.

Two operations:

* :meth:`MissionMemoryStore.write_memory` — embed the session's
  directive and ``PutItem`` the memory record (verdict, lessons,
  followups, provenance).
* :meth:`MissionMemoryStore.search_similar` — embed a query directive
  and ``SearchVectors`` the index for the closest past missions.

Resolution conventions, copied from their precedents:

* **Region** follows the store convention documented in
  ``gco/services/template_store.py``: ``DYNAMODB_REGION`` →
  ``GLOBAL_REGION`` → ``AWS_REGION``, else the SDK default chain.
* **Table / index names** resolve lazily from SSM
  (``/{project}/mission-memory-table-name`` and
  ``/{project}/mission-memory-index-name``) on first use and are cached
  on the instance — the same shape as
  :meth:`mcp.mission.state.DynamoDBBackend._resolve_table_name`. The
  SSM lookup goes through :func:`gco.services.aws_ssm.get_ssm_parameter`
  because ``gco_mcp/`` must not import ``cli/``.

Request-shape gotcha, verified against the botocore service model:
``SearchVectors``' ``SearchVector`` parameter is a **plain list** of
``{"N": "..."}`` attribute values. The ``{"L": [...]}`` wrapper is only
for writing ``directive_embedding`` on an item.

Failure contract: infrastructure that is absent or not yet queryable —
table or index missing, index still backfilling, SSM parameter never
published because the feature is disabled, no credentials, endpoint
unreachable — raises :class:`MissionMemoryUnavailableError`; everything
else raises :class:`MissionMemoryError`. ``SearchVectors`` errors while
the index is backfilling, so that case is deliberately part of
"unavailable", not a hard failure. Callers on the engine path swallow
both (memory is best-effort and must never fail a mission); the CLI
surfaces the message.

The runtime defaults below mirror the ``mission_memory`` block in
``cdk.json`` — the block drives the *deployed index*, these drive the
*vectors sent to it*, and ``tests/test_mission_memory_runtime.py``
asserts the two stay in agreement. ``dimensions`` in particular is a
one-way door: immutable after index creation, and query vectors must
come from the same model at the same width.
"""

from __future__ import annotations

import logging
import os
import time
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from gco.bedrock import get_default_embedding_model_id

from .embeddings import embed_text

logger = logging.getLogger(__name__)

__all__ = [
    "DEFAULT_DIMENSIONS",
    "DEFAULT_RETENTION_DAYS",
    "DEFAULT_TOP_K",
    "MEMORY_SCHEMA_VERSION",
    "MissionMemoryError",
    "MissionMemoryStore",
    "MissionMemoryUnavailableError",
]

#: Memory-item schema version, deliberately independent of the mission
#: session ``SCHEMA_VERSION`` — the two payloads evolve separately.
MEMORY_SCHEMA_VERSION = "1"

#: Runtime mirrors of the ``mission_memory`` defaults in ``cdk.json``.
DEFAULT_DIMENSIONS = 1024
DEFAULT_TOP_K = 3
DEFAULT_RETENTION_DAYS = 365

#: Region resolution order — the convention shared by every DynamoDB
#: store in the tree (see ``gco/services/template_store.py``).
_REGION_ENV_ORDER = ("DYNAMODB_REGION", "GLOBAL_REGION", "AWS_REGION")

_TABLE_NAME_PARAM_SUFFIX = "mission-memory-table-name"
_INDEX_NAME_PARAM_SUFFIX = "mission-memory-index-name"

_UNAVAILABLE_HINT = (
    "mission memory is unavailable — the table or index may not be "
    "provisioned (deploy with mission_memory.enabled: true), or the "
    "vector index may still be backfilling"
)


class MissionMemoryError(RuntimeError):
    """A mission-memory operation failed."""


class MissionMemoryUnavailableError(MissionMemoryError):
    """The mission-memory infrastructure is absent or not yet queryable.

    Raised for the degradable cases: table/index not provisioned, SSM
    name parameters never published, the vector index still backfilling
    after creation, missing credentials, or an unreachable endpoint.
    Engine callers treat this as "no prior context"; the CLI shows the
    message so an operator can tell "nothing similar" from "not there".
    """


def _resolve_region() -> str | None:
    """Return the first configured region env var, else ``None``.

    ``None`` lets boto3 fall through to its own default chain, matching
    how the sibling stores behave on hosts with a configured profile.
    """
    for env_var in _REGION_ENV_ORDER:
        value = os.environ.get(env_var)
        if value:
            return value
    return None


def _plain(value: Any) -> Any:
    """Recursively convert boto3-deserialized values to JSON-friendly types.

    ``TypeDeserializer`` yields :class:`decimal.Decimal` for every
    ``N``; integral values become ``int`` and the rest ``float`` so the
    CLI and prompt builders can ``json.dumps`` results directly.
    """
    from decimal import Decimal

    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, list):
        return [_plain(entry) for entry in value]
    if isinstance(value, dict):
        return {key: _plain(entry) for key, entry in value.items()}
    return value


def _number_attr(value: float) -> dict[str, str]:
    """Render one vector component as a DynamoDB number attribute value."""
    return {"N": repr(float(value))}


class MissionMemoryStore:
    """Runtime client for the mission-memory table and its vector index.

    Constructor arguments exist for tests and for callers that already
    know the deployed names; with no arguments every name resolves
    lazily from SSM on first use, so constructing a store on a host
    without AWS credentials is free.

    Args:
        table_name: Table name override; ``None`` resolves from SSM.
        index_name: Vector-index name override; ``None`` resolves from SSM.
        region: Region override; ``None`` follows the
            ``DYNAMODB_REGION`` → ``GLOBAL_REGION`` → ``AWS_REGION``
            convention, then the SDK default chain.
        embedding_model_id: Embedding model override; ``None`` resolves
            the checked-in default lazily on first embed.
        dimensions: Requested embedding width. Must match the deployed
            index width (one-way door); the default mirrors the
            ``mission_memory.dimensions`` default in ``cdk.json``.
        retention_days: TTL window written on new memory items; the
            default mirrors ``mission_memory.retention_days``.
    """

    def __init__(
        self,
        table_name: str | None = None,
        index_name: str | None = None,
        *,
        region: str | None = None,
        embedding_model_id: str | None = None,
        dimensions: int = DEFAULT_DIMENSIONS,
        retention_days: int = DEFAULT_RETENTION_DAYS,
    ) -> None:
        self._table_name = table_name
        self._index_name = index_name
        self._region = region
        self._embedding_model_id = embedding_model_id
        self._dimensions = int(dimensions)
        self._retention_days = int(retention_days)
        self._client: Any = None

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
            return get_ssm_parameter(param_name, region=self._region or _resolve_region())
        except ClientError as err:
            code = err.response.get("Error", {}).get("Code")
            if code == "ParameterNotFound":
                raise MissionMemoryUnavailableError(
                    f"SSM parameter {param_name} not found — {_UNAVAILABLE_HINT}"
                ) from err
            raise MissionMemoryError(f"SSM lookup for {param_name} failed: {err}") from err
        except BotoCoreError as err:
            # Covers NoCredentialsError and endpoint-unreachable faults.
            raise MissionMemoryUnavailableError(
                f"SSM unreachable resolving {param_name} — {_UNAVAILABLE_HINT}"
            ) from err

    def _resolve_table_name(self) -> str:
        """Return the cached table name, fetching from SSM on first call."""
        if self._table_name is None:
            self._table_name = self._resolve_ssm_name(_TABLE_NAME_PARAM_SUFFIX)
        return self._table_name

    def _resolve_index_name(self) -> str:
        """Return the cached index name, fetching from SSM on first call."""
        if self._index_name is None:
            self._index_name = self._resolve_ssm_name(_INDEX_NAME_PARAM_SUFFIX)
        return self._index_name

    def _get_client(self) -> Any:
        """Return the cached low-level ``dynamodb`` client, building it lazily."""
        if self._client is None:
            import boto3

            self._client = boto3.client("dynamodb", region_name=self._region or _resolve_region())
        return self._client

    def _resolved_embedding_model_id(self) -> str:
        """Return the embedding model id, resolving the default lazily."""
        if self._embedding_model_id is None:
            self._embedding_model_id = get_default_embedding_model_id()
        return self._embedding_model_id

    def _embed(self, text: str) -> list[float]:
        """Embed ``text`` at the configured width and guard the one-way door."""
        vector = embed_text(
            text,
            model_id=self._resolved_embedding_model_id(),
            dimensions=self._dimensions,
        )
        if len(vector) != self._dimensions:
            raise MissionMemoryError(
                f"embedding width {len(vector)} does not match the configured "
                f"index width {self._dimensions}; the model "
                f"{self._resolved_embedding_model_id()!r} ignored the requested "
                "dimensions — align mission_memory.dimensions with the model's "
                "output width (both are one-way doors on the deployed index)"
            )
        return vector

    # ------------------------------------------------------------------ #
    # operations
    # ------------------------------------------------------------------ #

    def write_memory(
        self,
        session: Mapping[str, Any],
        verdict: str,
        reason: str,
        lessons: str,
        followups: list[str],
    ) -> None:
        """Embed the session's directive and persist one memory item.

        Args:
            session: The terminal session payload — canonically a
                :class:`mcp.mission.types.SessionState`, but any mapping
                carrying ``session_id`` / ``directive_text`` (plus the
                optional ``criteria`` / ``tool_allowlist`` /
                ``iterations`` / ``created_at`` / ``ended_at`` fields)
                works, which is what lets the backfill command replay
                Final_Reports.
            verdict: Terminal verdict label (``complete`` | ``terminate``).
            reason: Terminal verdict reason.
            lessons: The report's lessons paragraph — the sampled
                overlay when one was produced, else the templated text.
            followups: The report's recommended followups.

        Raises:
            MissionMemoryUnavailableError: Table absent / feature not
                provisioned / no credentials / endpoint unreachable.
            MissionMemoryError: Any other write failure.
            EmbeddingError: Propagated from the embedding call; callers
                on the engine path swallow it like the rest.
        """
        from botocore.exceptions import (
            BotoCoreError,
            ClientError,
            NoCredentialsError,
            PartialCredentialsError,
        )

        directive = str(session.get("directive_text") or "")
        vector = self._embed(directive)

        completed_at = str(session.get("ended_at") or datetime.now(UTC).isoformat())
        criteria = session.get("criteria") or []
        item: dict[str, Any] = {
            "session_id": {"S": str(session["session_id"])},
            "directive": {"S": directive},
            "directive_embedding": {"L": [_number_attr(v) for v in vector]},
            "lessons": {"S": str(lessons)},
            "recommended_followups": {"L": [{"S": str(f)} for f in followups]},
            "final_verdict": {"S": str(verdict)},
            "verdict_reason": {"S": str(reason)},
            "iteration_count": {"N": str(len(session.get("iterations") or []))},
            "criteria_summary": {"L": [{"S": str(c.get("criterion_id", ""))} for c in criteria]},
            "tool_allowlist": {"L": [{"S": str(t)} for t in session.get("tool_allowlist") or []]},
            "created_at": {"S": str(session.get("created_at") or "")},
            "completed_at": {"S": completed_at},
            "embedding_model_id": {"S": self._resolved_embedding_model_id()},
            "embedding_dimensions": {"N": str(len(vector))},
            "schema_version": {"S": MEMORY_SCHEMA_VERSION},
            "ttl": {"N": str(int(time.time()) + self._retention_days * 86400)},
        }

        try:
            self._get_client().put_item(TableName=self._resolve_table_name(), Item=item)
        except (NoCredentialsError, PartialCredentialsError) as err:
            raise MissionMemoryUnavailableError(
                f"no AWS credentials — {_UNAVAILABLE_HINT}"
            ) from err
        except ClientError as err:
            code = err.response.get("Error", {}).get("Code")
            if code == "ResourceNotFoundException":
                raise MissionMemoryUnavailableError(
                    f"table {self._table_name!r} not found — {_UNAVAILABLE_HINT}"
                ) from err
            raise MissionMemoryError(f"mission-memory write failed: {err}") from err
        except BotoCoreError as err:
            raise MissionMemoryUnavailableError(
                f"DynamoDB unreachable — {_UNAVAILABLE_HINT}"
            ) from err

    def search_similar(
        self,
        directive: str,
        top_k: int = DEFAULT_TOP_K,
        final_verdict: str | None = None,
    ) -> list[dict[str, Any]]:
        """Return the closest past missions to ``directive``.

        Args:
            directive: Query text; embedded with the same model and
                width the index was built for.
            top_k: Number of results to request. The default mirrors
                ``mission_memory.top_k`` in ``cdk.json``.
            final_verdict: When set, an inline equality filter on the
                memory item's ``final_verdict`` (``complete`` |
                ``terminate``) — the one attribute the index declares as
                an ``INLINE_FILTER``.

        Returns:
            A list of plain dicts — the index's ``INCLUDE`` projection
            (``directive``, ``lessons``, ``recommended_followups``,
            ``final_verdict``, ``verdict_reason``, ``iteration_count``,
            ``completed_at``) plus ``session_id`` and a float ``score``
            (the distance-function score reported by DynamoDB).

        Raises:
            MissionMemoryUnavailableError: Table/index absent, index
                still backfilling, no credentials, or endpoint
                unreachable.
            MissionMemoryError: ``top_k`` < 1 or any other failure.
            EmbeddingError: Propagated from the embedding call.
        """
        from botocore.exceptions import (
            BotoCoreError,
            ClientError,
            NoCredentialsError,
            PartialCredentialsError,
        )

        if int(top_k) < 1:
            raise MissionMemoryError(f"top_k must be a positive integer, got {top_k!r}")

        vector = self._embed(directive)

        # Request-shape gotcha: SearchVector is a plain list of number
        # attribute values — the {"L": ...} wrapper is a write-side shape.
        request: dict[str, Any] = {
            "TableName": self._resolve_table_name(),
            "IndexName": self._resolve_index_name(),
            "SearchVector": [_number_attr(v) for v in vector],
            "TopK": int(top_k),
        }
        if final_verdict is not None:
            request["SearchConditionExpression"] = "final_verdict = :final_verdict"
            request["ExpressionAttributeValues"] = {":final_verdict": {"S": str(final_verdict)}}

        try:
            response = self._get_client().search_vectors(**request)
        except (NoCredentialsError, PartialCredentialsError) as err:
            raise MissionMemoryUnavailableError(
                f"no AWS credentials — {_UNAVAILABLE_HINT}"
            ) from err
        except ClientError as err:
            code = err.response.get("Error", {}).get("Code")
            if code in ("ResourceNotFoundException", "ValidationException"):
                # ValidationException is what SearchVectors answers while
                # the index is still backfilling after creation.
                raise MissionMemoryUnavailableError(
                    f"SearchVectors failed ({code}) — {_UNAVAILABLE_HINT}: {err}"
                ) from err
            raise MissionMemoryError(f"mission-memory search failed: {err}") from err
        except BotoCoreError as err:
            raise MissionMemoryUnavailableError(
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
            # recreated index with a different projection must not bloat
            # prompts or CLI output with a thousand floats.
            item.pop("directive_embedding", None)
            score = entry.get("Score")
            if score is not None:
                item["score"] = float(score)
            results.append(item)
        return results
