"""Unit tests for the mission-memory runtime modules.

Covers ``gco_mcp/mission/embeddings.py`` and ``gco_mcp/mission/memory.py``
with mocked boto3 — no live AWS. The two request-shape gotchas from the
design get explicit assertions: ``SearchVector`` must be a *plain list*
of ``{"N": ...}`` attribute values (never wrapped in ``{"L": ...}``),
while the written ``directive_embedding`` item attribute *is* the
``L``-wrapped form. Error-taxonomy tests pin the degradation contract:
absent tables, a backfilling index, missing SSM parameters, and missing
credentials all surface as ``MissionMemoryUnavailableError`` so the
engine can treat them as "no prior context" and the CLI can explain
them, while genuine request bugs stay ``MissionMemoryError``.

A skip-marked Floci placeholder records that the emulator does not
implement ``SearchVectors``, following the DynamoDB-backend precedent
in ``tests/test_mission_state.py``.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Any
from unittest import mock

import pytest
from botocore.exceptions import ClientError, NoCredentialsError
from botocore.exceptions import ConnectionError as BotocoreConnectionError

from gco.bedrock import (
    BedrockFTUFormNotAcceptedError,
    get_default_embedding_model_id,
)

# Mirror the import pattern used by every other ``test_mission_*`` module:
# ``gco_mcp/run_mcp.py`` adds ``gco_mcp/`` to ``sys.path`` at runtime, but tests
# have to do the same before importing ``mission.*``.
sys.path.insert(0, str(Path(__file__).parent.parent / "gco_mcp"))
from mission import embeddings as embeddings_module  # noqa: E402
from mission import memory as memory_module  # noqa: E402
from mission.embeddings import EmbeddingError, embed_text  # noqa: E402
from mission.memory import (  # noqa: E402
    DEFAULT_DIMENSIONS,
    DEFAULT_RETENTION_DAYS,
    DEFAULT_TOP_K,
    MEMORY_SCHEMA_VERSION,
    MissionMemoryError,
    MissionMemoryStore,
    MissionMemoryUnavailableError,
)

_REPO_ROOT = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# Fakes and helpers
# ---------------------------------------------------------------------------


class _FakeBody:
    """Duck-type of the ``StreamingBody`` in an ``invoke_model`` response."""

    def __init__(self, raw: bytes) -> None:
        self._raw = raw

    def read(self) -> bytes:
        return self._raw


class _FakeBedrockClient:
    """Records ``invoke_model`` calls; returns a canned payload or raises."""

    def __init__(self, payload: Any = None, raises: Exception | None = None) -> None:
        self._payload = payload
        self._raises = raises
        self.invoke_calls: list[dict[str, Any]] = []

    def invoke_model(self, **kwargs: Any) -> dict[str, Any]:
        self.invoke_calls.append(kwargs)
        if self._raises is not None:
            raise self._raises
        raw = (
            self._payload
            if isinstance(self._payload, bytes)
            else json.dumps(self._payload).encode()
        )
        return {"body": _FakeBody(raw)}


def _patch_boto3_session(client: Any) -> mock._patch:
    """Patch the lazy ``boto3`` import in ``embeddings._build_client``.

    Same pattern as ``tests/test_mission_sampling.py``: the module
    imports ``boto3`` *inside* the function, so a fake module is
    injected through ``sys.modules``.
    """
    fake_session = mock.MagicMock()
    fake_session.client.return_value = client
    fake_boto3 = mock.MagicMock()
    fake_boto3.Session.return_value = fake_session
    return mock.patch.dict("sys.modules", {"boto3": fake_boto3})


class _FakeDynamoClient:
    """Records ``put_item`` / ``search_vectors`` calls; replays or raises."""

    def __init__(
        self,
        search_response: dict[str, Any] | None = None,
        put_raises: Exception | None = None,
        search_raises: Exception | None = None,
    ) -> None:
        self._search_response = search_response or {"SearchResults": []}
        self._put_raises = put_raises
        self._search_raises = search_raises
        self.put_calls: list[dict[str, Any]] = []
        self.search_calls: list[dict[str, Any]] = []

    def put_item(self, **kwargs: Any) -> dict[str, Any]:
        self.put_calls.append(kwargs)
        if self._put_raises is not None:
            raise self._put_raises
        return {}

    def search_vectors(self, **kwargs: Any) -> dict[str, Any]:
        self.search_calls.append(kwargs)
        if self._search_raises is not None:
            raise self._search_raises
        return self._search_response


def _client_error(code: str, operation: str) -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": f"fake {code}"}}, operation)


def _make_store(
    client: _FakeDynamoClient,
    monkeypatch: pytest.MonkeyPatch,
    vector: list[float] | None = None,
    **kwargs: Any,
) -> MissionMemoryStore:
    """A store with injected names, a fake client, and a stubbed embedder."""
    resolved_vector = vector if vector is not None else [0.1, -0.2, 0.3]
    kwargs.setdefault("dimensions", len(resolved_vector))
    kwargs.setdefault("embedding_model_id", "unit-test-embed-model")
    store = MissionMemoryStore("memory-table", "memory-index", **kwargs)
    store._client = client
    monkeypatch.setattr(memory_module, "embed_text", lambda text, **_: list(resolved_vector))
    return store


def _session(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "session_id": "mission-0001",
        "directive_text": "Reduce validation loss below 0.5 on the demo model",
        "criteria": [
            {"criterion_id": "loss-under-half", "kind": "metric_threshold"},
            {"criterion_id": "loss-stable", "kind": "predicate"},
        ],
        "tool_allowlist": ["find_examples", "find_docs"],
        "created_at": "2026-08-10T12:00:00+00:00",
        "ended_at": "2026-08-10T12:34:56+00:00",
        "iterations": [{}, {}, {}],
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# embeddings.embed_text
# ---------------------------------------------------------------------------


class TestEmbedText:
    def test_happy_path_request_and_response_shape(self) -> None:
        client = _FakeBedrockClient(payload={"embedding": [0.25, -1, 2]})
        with _patch_boto3_session(client):
            vector = embed_text("hello", model_id="explicit-model", dimensions=512)

        assert vector == [0.25, -1.0, 2.0]
        assert all(isinstance(v, float) for v in vector)
        (call,) = client.invoke_calls
        assert call["modelId"] == "explicit-model"
        assert call["contentType"] == "application/json"
        assert call["accept"] == "application/json"
        assert json.loads(call["body"]) == {"inputText": "hello", "dimensions": 512}

    def test_omits_dimensions_when_none(self) -> None:
        client = _FakeBedrockClient(payload={"embedding": [0.5]})
        with _patch_boto3_session(client):
            embed_text("hello", model_id="explicit-model")

        (call,) = client.invoke_calls
        assert json.loads(call["body"]) == {"inputText": "hello"}

    def test_resolves_default_model_id_from_canonical_config(self) -> None:
        # No model_id argument: the checked-in default must flow through —
        # the same accessor the deployed index's documentation points at.
        client = _FakeBedrockClient(payload={"embedding": [0.5]})
        with _patch_boto3_session(client):
            embed_text("hello")

        (call,) = client.invoke_calls
        assert call["modelId"] == get_default_embedding_model_id()

    @pytest.mark.parametrize("text", ["", "   ", "\n\t"])
    def test_empty_text_raises_before_any_client_work(self, text: str) -> None:
        # No boto3 patch: reaching the client would ImportError-free but
        # build a real session; failing first proves the validation order.
        with (
            mock.patch.object(
                embeddings_module, "_build_client", side_effect=AssertionError("client built")
            ),
            pytest.raises(EmbeddingError, match="embedding_empty_text"),
        ):
            embed_text(text, model_id="explicit-model")

    def test_client_error_maps_to_coded_embedding_error(self) -> None:
        client = _FakeBedrockClient(raises=_client_error("ThrottlingException", "InvokeModel"))
        with (
            _patch_boto3_session(client),
            pytest.raises(EmbeddingError, match="embedding_bedrock_ThrottlingException"),
        ):
            embed_text("hello", model_id="explicit-model")

    def test_ftu_gate_escalates_instead_of_wrapping(self) -> None:
        client = _FakeBedrockClient(raises=_client_error("FTUFormNotFilled", "InvokeModel"))
        with _patch_boto3_session(client), pytest.raises(BedrockFTUFormNotAcceptedError):
            embed_text("hello", model_id="explicit-model")

    @pytest.mark.parametrize(
        "payload",
        [
            b"not json",
            {"no_embedding_key": 1},
            {"embedding": []},
            {"embedding": ["x", "y"]},
            {"embedding": [True, False]},
            {"embedding": "0.5"},
        ],
    )
    def test_malformed_response_raises(self, payload: Any) -> None:
        client = _FakeBedrockClient(payload=payload)
        with (
            _patch_boto3_session(client),
            pytest.raises(EmbeddingError, match="embedding_malformed_response"),
        ):
            embed_text("hello", model_id="explicit-model")

    def test_no_credentials_at_client_build(self) -> None:
        fake_session = mock.MagicMock()
        fake_session.client.side_effect = NoCredentialsError()
        fake_boto3 = mock.MagicMock()
        fake_boto3.Session.return_value = fake_session
        with (
            mock.patch.dict("sys.modules", {"boto3": fake_boto3}),
            pytest.raises(EmbeddingError, match="embedding_no_credentials"),
        ):
            embed_text("hello", model_id="explicit-model")


# ---------------------------------------------------------------------------
# memory.MissionMemoryStore — write path
# ---------------------------------------------------------------------------


class TestWriteMemory:
    def test_item_shape(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client = _FakeDynamoClient()
        store = _make_store(client, monkeypatch, vector=[0.1, -0.2, 0.3])

        before = int(time.time())
        store.write_memory(
            _session(),
            "complete",
            "criteria_met",
            "Lessons paragraph.",
            ["Follow up A", "Follow up B"],
        )
        after = int(time.time())

        (call,) = client.put_calls
        assert call["TableName"] == "memory-table"
        item = call["Item"]
        assert item["session_id"] == {"S": "mission-0001"}
        assert item["directive"] == {"S": "Reduce validation loss below 0.5 on the demo model"}
        # Write-side shape: the vector attribute IS the L-wrapped form.
        assert item["directive_embedding"] == {"L": [{"N": "0.1"}, {"N": "-0.2"}, {"N": "0.3"}]}
        assert item["lessons"] == {"S": "Lessons paragraph."}
        assert item["recommended_followups"] == {"L": [{"S": "Follow up A"}, {"S": "Follow up B"}]}
        assert item["final_verdict"] == {"S": "complete"}
        assert item["verdict_reason"] == {"S": "criteria_met"}
        assert item["iteration_count"] == {"N": "3"}
        assert item["criteria_summary"] == {"L": [{"S": "loss-under-half"}, {"S": "loss-stable"}]}
        assert item["tool_allowlist"] == {"L": [{"S": "find_examples"}, {"S": "find_docs"}]}
        assert item["created_at"] == {"S": "2026-08-10T12:00:00+00:00"}
        assert item["completed_at"] == {"S": "2026-08-10T12:34:56+00:00"}
        assert item["embedding_model_id"] == {"S": "unit-test-embed-model"}
        assert item["embedding_dimensions"] == {"N": "3"}
        assert item["schema_version"] == {"S": MEMORY_SCHEMA_VERSION}
        ttl = int(item["ttl"]["N"])
        assert (
            before + DEFAULT_RETENTION_DAYS * 86400 <= ttl <= after + DEFAULT_RETENTION_DAYS * 86400
        )

    def test_completed_at_falls_back_to_now(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client = _FakeDynamoClient()
        store = _make_store(client, monkeypatch)
        session = _session()
        del session["ended_at"]

        store.write_memory(session, "terminate", "max_iterations", "L", [])

        (call,) = client.put_calls
        completed_at = call["Item"]["completed_at"]["S"]
        assert completed_at  # ISO-8601 from the clock, never empty
        assert "T" in completed_at

    def test_resource_not_found_is_unavailable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client = _FakeDynamoClient(put_raises=_client_error("ResourceNotFoundException", "PutItem"))
        store = _make_store(client, monkeypatch)
        with pytest.raises(MissionMemoryUnavailableError, match="not found"):
            store.write_memory(_session(), "complete", "criteria_met", "L", [])

    def test_validation_error_on_write_is_a_hard_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # On the write path ValidationException means a malformed item — a
        # bug, not absent infrastructure — so it must NOT map to unavailable.
        client = _FakeDynamoClient(put_raises=_client_error("ValidationException", "PutItem"))
        store = _make_store(client, monkeypatch)
        with pytest.raises(MissionMemoryError) as excinfo:
            store.write_memory(_session(), "complete", "criteria_met", "L", [])
        assert not isinstance(excinfo.value, MissionMemoryUnavailableError)

    def test_no_credentials_is_unavailable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client = _FakeDynamoClient(put_raises=NoCredentialsError())
        store = _make_store(client, monkeypatch)
        with pytest.raises(MissionMemoryUnavailableError, match="credentials"):
            store.write_memory(_session(), "complete", "criteria_met", "L", [])


# ---------------------------------------------------------------------------
# memory.MissionMemoryStore — search path
# ---------------------------------------------------------------------------


class TestSearchSimilar:
    def test_request_shape_plain_list_not_l_wrapped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client = _FakeDynamoClient()
        store = _make_store(client, monkeypatch, vector=[0.5, 1.0])

        store.search_similar("similar directive")

        (call,) = client.search_calls
        assert call["TableName"] == "memory-table"
        assert call["IndexName"] == "memory-index"
        assert call["TopK"] == DEFAULT_TOP_K
        # The design's request-shape gotcha, pinned: a plain list of
        # number attribute values — not {"L": [...]}.
        assert call["SearchVector"] == [{"N": "0.5"}, {"N": "1.0"}]
        assert not isinstance(call["SearchVector"], dict)
        assert "SearchConditionExpression" not in call
        assert "ExpressionAttributeValues" not in call

    def test_final_verdict_inline_filter(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client = _FakeDynamoClient()
        store = _make_store(client, monkeypatch)

        store.search_similar("directive", top_k=5, final_verdict="complete")

        (call,) = client.search_calls
        assert call["TopK"] == 5
        assert call["SearchConditionExpression"] == "final_verdict = :final_verdict"
        assert call["ExpressionAttributeValues"] == {":final_verdict": {"S": "complete"}}

    def test_deserialises_results_to_plain_dicts(self, monkeypatch: pytest.MonkeyPatch) -> None:
        response = {
            "SearchResults": [
                {
                    "Item": {
                        "session_id": {"S": "mission-0009"},
                        "directive": {"S": "old directive"},
                        "lessons": {"S": "what we learned"},
                        "recommended_followups": {"L": [{"S": "next step"}]},
                        "final_verdict": {"S": "complete"},
                        "verdict_reason": {"S": "criteria_met"},
                        "iteration_count": {"N": "4"},
                        "completed_at": {"S": "2026-08-01T00:00:00+00:00"},
                        "directive_embedding": {"L": [{"N": "0.1"}]},
                    },
                    "Score": 0.87,
                }
            ]
        }
        client = _FakeDynamoClient(search_response=response)
        store = _make_store(client, monkeypatch)

        results = store.search_similar("directive")

        (result,) = results
        assert result["session_id"] == "mission-0009"
        assert result["lessons"] == "what we learned"
        assert result["recommended_followups"] == ["next step"]
        assert result["iteration_count"] == 4  # Decimal -> int
        assert isinstance(result["iteration_count"], int)
        assert result["score"] == pytest.approx(0.87)
        # Never surface the raw vector, even if a future projection adds it.
        assert "directive_embedding" not in result

    @pytest.mark.parametrize("code", ["ResourceNotFoundException", "ValidationException"])
    def test_absent_or_backfilling_index_is_unavailable(
        self, code: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = _FakeDynamoClient(search_raises=_client_error(code, "SearchVectors"))
        store = _make_store(client, monkeypatch)
        with pytest.raises(MissionMemoryUnavailableError, match="backfilling"):
            store.search_similar("directive")

    def test_other_client_error_is_a_hard_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client = _FakeDynamoClient(
            search_raises=_client_error("AccessDeniedException", "SearchVectors")
        )
        store = _make_store(client, monkeypatch)
        with pytest.raises(MissionMemoryError) as excinfo:
            store.search_similar("directive")
        assert not isinstance(excinfo.value, MissionMemoryUnavailableError)

    def test_endpoint_unreachable_is_unavailable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client = _FakeDynamoClient(search_raises=BotocoreConnectionError(error="host unreachable"))
        store = _make_store(client, monkeypatch)
        with pytest.raises(MissionMemoryUnavailableError, match="unreachable"):
            store.search_similar("directive")

    def test_top_k_guard_fires_before_embedding(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client = _FakeDynamoClient()
        store = MissionMemoryStore(
            "memory-table", "memory-index", embedding_model_id="m", dimensions=3
        )
        store._client = client
        monkeypatch.setattr(
            memory_module,
            "embed_text",
            lambda *a, **k: pytest.fail("embed_text must not be called"),
        )
        with pytest.raises(MissionMemoryError, match="top_k"):
            store.search_similar("directive", top_k=0)
        assert client.search_calls == []

    def test_embedding_error_propagates_untouched(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client = _FakeDynamoClient()
        store = _make_store(client, monkeypatch)

        def _boom(*args: Any, **kwargs: Any) -> list[float]:
            raise EmbeddingError("embedding_transport_failure")

        monkeypatch.setattr(memory_module, "embed_text", _boom)
        with pytest.raises(EmbeddingError, match="embedding_transport_failure"):
            store.search_similar("directive")
        assert client.search_calls == []

    def test_dimension_mismatch_guard(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client = _FakeDynamoClient()
        store = _make_store(client, monkeypatch, vector=[0.1, 0.2], dimensions=3)
        with pytest.raises(MissionMemoryError, match="does not match"):
            store.search_similar("directive")
        assert client.search_calls == []


# ---------------------------------------------------------------------------
# Name / region resolution
# ---------------------------------------------------------------------------


class TestResolution:
    def test_missing_ssm_parameter_is_unavailable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("GCO_PROJECT_NAME", "widget")

        def _raise(name: str, **kwargs: Any) -> str:
            raise _client_error("ParameterNotFound", "GetParameter")

        monkeypatch.setattr("gco.services.aws_ssm.get_ssm_parameter", _raise)
        store = MissionMemoryStore()
        with pytest.raises(
            MissionMemoryUnavailableError, match="/widget/mission-memory-table-name"
        ):
            store._resolve_table_name()

    def test_ssm_names_resolve_lazily_and_cache(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("GCO_PROJECT_NAME", raising=False)
        calls: list[str] = []

        def _fake(name: str, **kwargs: Any) -> str:
            calls.append(name)
            return f"resolved:{name}"

        monkeypatch.setattr("gco.services.aws_ssm.get_ssm_parameter", _fake)
        store = MissionMemoryStore()
        assert calls == []  # constructor must not touch SSM
        assert store._resolve_table_name() == "resolved:/gco/mission-memory-table-name"
        assert store._resolve_index_name() == "resolved:/gco/mission-memory-index-name"
        store._resolve_table_name()
        store._resolve_index_name()
        assert calls == [
            "/gco/mission-memory-table-name",
            "/gco/mission-memory-index-name",
        ]

    def test_constructor_names_bypass_ssm(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            "gco.services.aws_ssm.get_ssm_parameter",
            lambda *a, **k: pytest.fail("SSM must not be called"),
        )
        store = MissionMemoryStore("explicit-table", "explicit-index")
        assert store._resolve_table_name() == "explicit-table"
        assert store._resolve_index_name() == "explicit-index"

    def test_region_resolution_order(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for env_var in ("DYNAMODB_REGION", "GLOBAL_REGION", "AWS_REGION"):
            monkeypatch.delenv(env_var, raising=False)
        assert memory_module._resolve_region() is None

        monkeypatch.setenv("AWS_REGION", "eu-west-1")
        assert memory_module._resolve_region() == "eu-west-1"

        monkeypatch.setenv("GLOBAL_REGION", "us-east-2")
        assert memory_module._resolve_region() == "us-east-2"

        monkeypatch.setenv("DYNAMODB_REGION", "us-west-2")
        assert memory_module._resolve_region() == "us-west-2"


# ---------------------------------------------------------------------------
# Runtime defaults mirror cdk.json
# ---------------------------------------------------------------------------


class TestDefaultsMirrorConfig:
    def test_runtime_defaults_agree_with_cdk_json(self) -> None:
        # cdk.json's mission_memory block drives the deployed index; the
        # runtime constants drive the vectors sent to it. Drift between
        # them is the "meaningless results" failure mode the design's
        # one-way-door warning is about, so it fails here.
        cdk_json = json.loads((_REPO_ROOT / "cdk.json").read_text())
        documented = cdk_json["context"]["mission_memory"]
        assert documented["dimensions"] == DEFAULT_DIMENSIONS
        assert documented["top_k"] == DEFAULT_TOP_K
        assert documented["retention_days"] == DEFAULT_RETENTION_DAYS


# ---------------------------------------------------------------------------
# Floci placeholder
# ---------------------------------------------------------------------------


@pytest.mark.skip(reason="Floci does not implement SearchVectors")
def test_floci_search_vectors_placeholder() -> None:
    """Placeholder so the deferred Floci coverage is visible in pytest output.

    The Floci emulator layer (``GCO_FLOCI_ENDPOINT``, see
    ``docs/FLOCI_TESTING.md``) exercises the production DynamoDB stores
    over the real wire protocol, but it does not implement the
    ``SearchVectors`` API, so the mission-memory search path cannot be
    emulator-tested. An AWS-credentialed smoke test covers it instead.
    """
