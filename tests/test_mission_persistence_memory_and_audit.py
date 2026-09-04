"""Targeted Mission runtime persistence, memory, embedding, and audit tests."""

from __future__ import annotations

import sys
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from botocore.exceptions import ClientError, EndpointConnectionError

PROJECT_ROOT = Path(__file__).resolve().parents[1]
GCO_MCP_ROOT = str(PROJECT_ROOT / "gco_mcp")
if GCO_MCP_ROOT not in sys.path:
    sys.path.insert(0, GCO_MCP_ROOT)

from mission import SCHEMA_VERSION, audit, embeddings, final_report, memory, state  # noqa: E402


def _session(**overrides: Any) -> dict[str, Any]:
    value: dict[str, Any] = {
        "version": SCHEMA_VERSION,
        "session_id": "persistence-coverage",
        "directive_text": "Find capacity.",
        "criteria": [],
        "budget": {"max_iterations": 1, "max_wall_clock_seconds": 60},
        "tool_allowlist": ["tool"],
        "checkpoint_cadence": {"kind": "every_iteration"},
        "stagnation_threshold": 3,
        "use_sampling": False,
        "allow_scripted_strategies": False,
        "status": "terminated",
        "created_at": "2025-01-01T00:00:00+00:00",
        "started_at": "2025-01-01T00:00:00+00:00",
        "ended_at": "2025-01-01T00:00:01+00:00",
        "iterations": [],
        "no_progress_counter": 0,
    }
    value.update(overrides)
    return value


def _client_error(code: str, operation: str = "Operation") -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": "denied"}}, operation)


def test_final_report_nonfilesystem_attach_and_strip_helpers() -> None:
    class Backend:
        def __init__(self) -> None:
            self.saved: list[dict[str, Any]] = []

        def save_session(self, session: dict[str, Any]) -> None:
            self.saved.append(session)

    backend = Backend()
    session = _session()
    path = final_report.write_final_report(
        backend,
        session,
        "terminate",
        "user_abort",
    )
    assert path == "dynamodb://persistence-coverage/report"
    assert backend.saved == [session]
    assert session["final_report"]["final_verdict"] == "terminate"

    malformed: list[Any] = ["keep", {"criterion_id": "c", "_parsed_ast": object()}]
    cleaned = final_report._strip_parsed_ast_from_criteria(malformed)  # type: ignore[arg-type]
    assert cleaned[0] == "keep"
    assert "_parsed_ast" not in cleaned[1]

    nested = {"outer": [{"_parsed_ast": object(), "value": 1}]}
    final_report._strip_parsed_ast_in_place(nested)
    assert nested == {"outer": [{"value": 1}]}

    assert final_report._final_criteria_evaluation(_session()) is None
    assert (
        final_report._final_criteria_evaluation(_session(iterations=[{"criteria_evaluation": []}]))
        is None
    )
    assert (
        final_report._safely_invoke_sampler(
            lambda *_args: None,
            _session(),
            "terminate",
            "user_abort",
        )
        is None
    )


def test_final_report_windows_skip_and_oserror_translation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = state.FilesystemBackend(root=tmp_path)
    original_os_name = final_report.os.name
    monkeypatch.setattr(final_report.os, "name", "nt")
    path = final_report._write_report_to_filesystem(backend, "windows", {"ok": True})
    monkeypatch.setattr(final_report.os, "name", original_os_name)
    assert Path(path).exists()

    original = PermissionError("replace denied")

    def fail_replace(_src: Any, _dst: Any) -> None:
        raise original

    monkeypatch.setattr(final_report.os, "replace", fail_replace)
    with pytest.raises(OSError, match="replace denied") as exc_info:
        final_report._write_report_to_filesystem(backend, "failure", {"ok": True})
    assert exc_info.value.__cause__ is original


def test_state_protocol_defaults_and_filesystem_error_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    protocol_target = object()
    assert state.MissionStateBackend.load_session(protocol_target, "s") is None
    assert state.MissionStateBackend.save_session(protocol_target, _session()) is None
    assert state.MissionStateBackend.list_sessions(protocol_target) is None
    assert state.MissionStateBackend.delete_session(protocol_target, "s") is None

    backend = state.FilesystemBackend(root=tmp_path)

    def fail_read(_self: Path, *, encoding: str) -> str:
        raise PermissionError("read denied")

    monkeypatch.setattr(Path, "read_text", fail_read)
    assert backend.load_session("s") is None
    monkeypatch.undo()

    tmp_path.mkdir(exist_ok=True)
    assert backend.delete_session("missing") is False

    def fail_remove(_path: Any) -> None:
        raise PermissionError("delete denied")

    monkeypatch.setattr(state.os, "remove", fail_remove)
    with pytest.raises(PermissionError, match="delete denied"):
        backend.delete_session("s")


def test_state_windows_permission_branches_and_dynamodb_constructor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = state.FilesystemBackend(root=tmp_path / "windows")
    monkeypatch.setattr(state.os, "name", "nt")
    backend._ensure_root()
    backend.save_session(_session(session_id="windows"))
    assert backend.load_session("windows") is not None

    dynamo = state.DynamoDBBackend("table")
    assert dynamo._table_name == "table"
    assert dynamo._table is None


def test_memory_plain_ssm_and_lazy_resolution_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert memory._plain(Decimal("1.25")) == 1.25

    import gco.services.aws_ssm as aws_ssm

    store = memory.MissionMemoryStore()
    monkeypatch.setattr(
        aws_ssm,
        "get_ssm_parameter",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(_client_error("AccessDeniedException")),
    )
    with pytest.raises(memory.MissionMemoryError, match="SSM lookup"):
        store._resolve_ssm_name("name")

    transport_error = EndpointConnectionError(endpoint_url="https://ssm.invalid")
    monkeypatch.setattr(
        aws_ssm,
        "get_ssm_parameter",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(transport_error),
    )
    with pytest.raises(memory.MissionMemoryUnavailableError, match="SSM unreachable"):
        store._resolve_ssm_name("name")

    client = object()
    monkeypatch.setitem(
        sys.modules, "boto3", SimpleNamespace(client=lambda *_args, **_kwargs: client)
    )
    assert store._get_client() is client
    assert store._get_client() is client

    monkeypatch.setattr(memory, "get_default_embedding_model_id", lambda: "embed-model")
    assert store._resolved_embedding_model_id() == "embed-model"
    assert store._resolved_embedding_model_id() == "embed-model"


def test_memory_write_search_and_list_error_translation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport_error = EndpointConnectionError(endpoint_url="https://dynamodb.invalid")

    class WriteClient:
        def put_item(self, **_kwargs: Any) -> None:
            raise transport_error

    write_store = memory.MissionMemoryStore(
        table_name="table",
        index_name="index",
        embedding_model_id="embed",
        dimensions=1,
    )
    monkeypatch.setattr(write_store, "_embed", lambda _text: [0.5])
    monkeypatch.setattr(write_store, "_get_client", lambda: WriteClient())
    with pytest.raises(memory.MissionMemoryUnavailableError, match="DynamoDB unreachable"):
        write_store.write_memory(_session(), "terminate", "reason", "lessons", [])

    class SearchDenied:
        def search_vectors(self, **_kwargs: Any) -> Any:
            raise _client_error("AccessDeniedException", "SearchVectors")

    search_store = memory.MissionMemoryStore(
        table_name="table",
        index_name="index",
        embedding_model_id="embed",
        dimensions=1,
    )
    monkeypatch.setattr(search_store, "_embed", lambda _text: [0.5])
    monkeypatch.setattr(search_store, "_get_client", lambda: SearchDenied())
    with pytest.raises(memory.MissionMemoryError, match="search failed"):
        search_store.search_similar("query")

    class SearchWithoutScore:
        def search_vectors(self, **_kwargs: Any) -> dict[str, Any]:
            return {"SearchResults": [{"Item": {}}]}

    monkeypatch.setattr(search_store, "_get_client", lambda: SearchWithoutScore())
    assert search_store.search_similar("query") == [{}]

    class ListClient:
        def scan(self, **_kwargs: Any) -> Any:
            raise transport_error

    list_store = memory.MissionMemoryStore(table_name="table")
    monkeypatch.setattr(list_store, "_get_client", lambda: ListClient())
    with pytest.raises(memory.MissionMemoryUnavailableError, match="DynamoDB unreachable"):
        list_store.list_memories()


def test_embedding_transport_error_is_stable(monkeypatch: pytest.MonkeyPatch) -> None:
    transport_error = EndpointConnectionError(endpoint_url="https://bedrock.invalid")

    class Client:
        def invoke_model(self, **_kwargs: Any) -> Any:
            raise transport_error

    monkeypatch.setattr(embeddings, "_build_client", lambda: Client())
    with pytest.raises(embeddings.EmbeddingError, match="embedding_transport_failure") as exc_info:
        embeddings.embed_text("directive", model_id="embed")
    assert exc_info.value.__cause__ is transport_error


def test_audit_optional_token_and_child_status_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    emitted: list[dict[str, Any]] = []
    monkeypatch.setattr(audit, "_emit", emitted.append)
    audit.emit_sampling_event(
        "s",
        0,
        "revision",
        "used",
        "bedrock",
        input_tokens=12,
        output_tokens=34,
    )
    assert emitted[-1]["input_tokens"] == 12
    assert emitted[-1]["output_tokens"] == 34

    audit.emit_child_lifecycle_event(
        "parent",
        "child",
        "slot",
        "terminal",
        final_status="completed",
    )
    assert emitted[-1]["final_status"] == "completed"


def test_audit_replay_no_active_iteration_and_ignored_event_loop() -> None:
    assert (
        audit.replay_audit_entries(
            "s",
            [
                {
                    "event_type": audit.EVENT_TYPE_VERDICT,
                    "mission_session_id": "s",
                    "verdict": "continue",
                }
            ],
        )
        == []
    )

    entries = [
        {
            "event_type": audit.EVENT_TYPE_SAMPLING,
            "mission_session_id": "s",
            "sampling_status": "used",
        },
        {
            "event_type": audit.EVENT_TYPE_PHASE,
            "mission_session_id": "s",
            "iteration_index": 0,
            "phase": "propose",
            "phase_status": "succeeded",
            "phase_started_at": "a",
            "phase_ended_at": "b",
        },
        {
            "event_type": audit.EVENT_TYPE_VERDICT,
            "mission_session_id": "s",
            "iteration_index": 0,
            "verdict": "continue",
            "verdict_reason": "in_progress",
        },
    ]
    replayed = audit.replay_audit_entries("s", entries)
    assert len(replayed) == 1
    assert replayed[0]["verdict"] == "continue"


def test_final_report_sampler_none_still_persists() -> None:
    class Backend:
        def __init__(self) -> None:
            self.saved = False

        def save_session(self, _session: dict[str, Any]) -> None:
            self.saved = True

    backend = Backend()
    session = _session(session_id="sampler-none")
    path = final_report.write_final_report(
        backend,
        session,
        "terminate",
        "user_abort",
        sampler=lambda *_args: None,
    )
    assert path == "dynamodb://sampler-none/report"
    assert backend.saved is True


def test_memory_recursive_plain_and_remaining_error_translations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert memory._plain([Decimal("2"), {"ratio": Decimal("1.5")}]) == [
        2,
        {"ratio": 1.5},
    ]

    transport_error = EndpointConnectionError(endpoint_url="https://dynamodb.invalid")

    class SearchTransportFailure:
        def search_vectors(self, **_kwargs: Any) -> Any:
            raise transport_error

    search_store = memory.MissionMemoryStore(
        table_name="table",
        index_name="index",
        embedding_model_id="embed",
        dimensions=1,
    )
    monkeypatch.setattr(search_store, "_embed", lambda _text: [0.5])
    monkeypatch.setattr(search_store, "_get_client", lambda: SearchTransportFailure())
    with pytest.raises(memory.MissionMemoryUnavailableError, match="DynamoDB unreachable"):
        search_store.search_similar("query")

    from botocore.exceptions import NoCredentialsError

    class SearchWithoutCredentials:
        def search_vectors(self, **_kwargs: Any) -> Any:
            raise NoCredentialsError()

    monkeypatch.setattr(search_store, "_get_client", lambda: SearchWithoutCredentials())
    with pytest.raises(memory.MissionMemoryUnavailableError, match="no AWS credentials"):
        search_store.search_similar("query")

    class ListDenied:
        def scan(self, **_kwargs: Any) -> Any:
            raise _client_error("AccessDeniedException", "Scan")

    list_store = memory.MissionMemoryStore(table_name="table")
    monkeypatch.setattr(list_store, "_get_client", lambda: ListDenied())
    with pytest.raises(memory.MissionMemoryError, match="list failed"):
        list_store.list_memories()


def test_audit_child_lifecycle_reason_is_preserved(monkeypatch: pytest.MonkeyPatch) -> None:
    emitted: list[dict[str, Any]] = []
    monkeypatch.setattr(audit, "_emit", emitted.append)

    audit.emit_child_lifecycle_event(
        "parent",
        "child",
        "worker-a",
        "respawn_denied",
        reason="child_iteration_pool_exhausted",
    )

    assert emitted[-1]["reason"] == "child_iteration_pool_exhausted"
