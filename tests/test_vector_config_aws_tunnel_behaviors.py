"""Behavior tests for vector, managed-config, AWS, and SSM tunnel edges."""

from __future__ import annotations

import io
import json
import subprocess
from contextlib import contextmanager
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, patch

import pytest
from botocore.exceptions import (
    ClientError,
    EndpointConnectionError,
    NoCredentialsError,
)

from cli import aws_client, managed_config, ssm_tunnel, vector_store
from cli.aws_client import ApiEndpoint, APIRequestError, GCOAWSClient, RegionalApiDiscoveryError
from cli.managed_config import (
    CODEX_REASONING_EFFORT,
    REGIONAL_DEPLOYMENT_REGIONS,
    ConfigMutationLockError,
    ManagedConfigError,
)
from cli.vector_store import VectorStoreClient, VectorStoreError, VectorStoreUnavailableError


def _client_error(code: str) -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": code}}, "Operation")


def _vector_client(**kwargs) -> VectorStoreClient:
    defaults = {
        "table_name": "vectors",
        "index_name": "embedding-index",
        "bucket_name": "corpus",
        "dimensions": 2,
    }
    defaults.update(kwargs)
    return VectorStoreClient(**defaults)


def _aws_client() -> GCOAWSClient:
    client = object.__new__(GCOAWSClient)
    client.config = SimpleNamespace(
        project_name="gco",
        api_gateway_region="us-east-1",
        api_gateway_stack_name="gco-api-gateway",
        regional_stack_prefix="gco",
        cache_ttl_seconds=300,
        default_region="us-east-1",
    )
    client._session = MagicMock()
    client._api_endpoint_cache = None
    client._regional_api_cache = {}
    client._regional_stacks_cache = None
    client._cache_timestamp = None
    client._use_regional_api = False
    return client


def test_vector_region_resolution_handles_configured_and_absent_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in vector_store._REGION_ENV_ORDER:
        monkeypatch.delenv(name, raising=False)
    assert vector_store._resolve_region() is None
    monkeypatch.setenv(vector_store._REGION_ENV_ORDER[0], "us-west-2")
    assert vector_store._resolve_region() == "us-west-2"


def test_vector_plain_recursively_converts_decimal_values() -> None:
    assert vector_store._plain({"count": Decimal("2"), "values": [Decimal("1.5")]}) == {
        "count": 2,
        "values": [1.5],
    }


def test_vector_ssm_nonmissing_client_error_is_hard_failure() -> None:
    client = VectorStoreClient()
    with (
        patch(
            "gco.services.aws_ssm.get_ssm_parameter",
            side_effect=_client_error("AccessDeniedException"),
        ),
        pytest.raises(VectorStoreError, match="SSM lookup"),
    ):
        client._resolve_ssm_name("vector-store/table-name")


def test_vector_bucket_resolution_fetches_name_and_region_once() -> None:
    client = VectorStoreClient()
    client._resolve_ssm_name = Mock(side_effect=["bucket", "us-west-2"])

    assert client._resolve_bucket() == ("bucket", "us-west-2")
    assert client._resolve_bucket() == ("bucket", "us-west-2")
    assert client._resolve_ssm_name.call_count == 2


@pytest.mark.parametrize(
    ("method", "service", "attribute", "args"),
    [
        ("_get_dynamodb_client", "dynamodb", "_dynamodb_client", ()),
        ("_get_bedrock_client", "bedrock-runtime", "_bedrock_client", ()),
        ("_get_s3_client", "s3", "_s3_client", ("us-west-2",)),
    ],
)
def test_vector_sdk_clients_are_created_lazily_and_cached(
    monkeypatch: pytest.MonkeyPatch,
    method: str,
    service: str,
    attribute: str,
    args: tuple[object, ...],
) -> None:
    client = _vector_client(query_region="eu-west-1")
    sdk_client = object()
    with patch("boto3.client", return_value=sdk_client) as create:
        assert getattr(client, method)(*args) is sdk_client
        assert getattr(client, method)(*args) is sdk_client

    assert getattr(client, attribute) is sdk_client
    assert create.call_count == 1
    assert create.call_args.args[0] == service


@pytest.mark.parametrize(
    ("error", "exception", "message"),
    [
        (NoCredentialsError(), VectorStoreUnavailableError, "no AWS credentials"),
        (_client_error("AccessDeniedException"), VectorStoreError, "bedrock AccessDeniedException"),
        (
            EndpointConnectionError(endpoint_url="https://bedrock"),
            VectorStoreUnavailableError,
            "Bedrock unreachable",
        ),
    ],
)
def test_vector_embedding_translates_sdk_failures(
    error: Exception,
    exception: type[Exception],
    message: str,
) -> None:
    client = _vector_client()
    bedrock = MagicMock()
    bedrock.invoke_model.side_effect = error
    client._bedrock_client = bedrock

    with pytest.raises(exception, match=message):
        client._embed("hello")


def test_vector_embedding_rejects_response_without_vector() -> None:
    client = _vector_client()
    bedrock = MagicMock()
    bedrock.invoke_model.return_value = {"body": io.BytesIO(b"{}")}
    client._bedrock_client = bedrock

    with pytest.raises(VectorStoreError, match="carried no vector"):
        client._embed("hello")


def test_vector_search_translates_missing_credentials() -> None:
    client = _vector_client()
    client._embed = Mock(return_value=[0.1, 0.2])
    dynamo = MagicMock()
    dynamo.search_vectors.side_effect = NoCredentialsError()
    client._dynamodb_client = dynamo

    with pytest.raises(VectorStoreUnavailableError, match="no AWS credentials"):
        client.search("hello")


def test_vector_search_omits_score_when_service_does_not_return_one() -> None:
    client = _vector_client()
    client._embed = Mock(return_value=[0.1, 0.2])
    dynamo = MagicMock()
    dynamo.search_vectors.return_value = {
        "SearchResults": [{"Item": {"source": {"S": "corpus/doc.md"}}}]
    }
    client._dynamodb_client = dynamo

    assert client.search("hello") == [{"source": "corpus/doc.md"}]


@pytest.mark.parametrize(
    ("error", "exception"),
    [
        (NoCredentialsError(), VectorStoreUnavailableError),
        (_client_error("AccessDeniedException"), VectorStoreError),
        (EndpointConnectionError(endpoint_url="https://dynamodb"), VectorStoreUnavailableError),
    ],
)
def test_vector_status_translates_sdk_failures(
    error: Exception, exception: type[Exception]
) -> None:
    client = _vector_client()
    dynamo = MagicMock()
    dynamo.describe_table.side_effect = error
    client._dynamodb_client = dynamo

    with pytest.raises(exception):
        client.status()


def test_vector_status_walks_all_vector_index_entries() -> None:
    client = _vector_client()
    client._dynamodb_client = MagicMock()
    client._dynamodb_client.describe_table.return_value = {
        "Table": {
            "TableStatus": "ACTIVE",
            "ItemCount": 2,
            "VectorIndexes": [
                {"IndexName": "embedding-index", "IndexStatus": "ACTIVE"},
                {"IndexName": "other", "IndexStatus": "BUILDING"},
            ],
            "Replicas": [],
        }
    }

    assert client.status()["index_status"] == "ACTIVE"


def test_vector_ingest_rejects_nonfile_path(tmp_path: Path) -> None:
    with pytest.raises(VectorStoreError, match="not a file"):
        _vector_client().ingest([tmp_path / "missing.md"])


@pytest.mark.parametrize(
    "error",
    [
        _client_error("AccessDeniedException"),
        EndpointConnectionError(endpoint_url="https://s3"),
    ],
)
def test_vector_ingest_translates_upload_failures(tmp_path: Path, error: Exception) -> None:
    document = tmp_path / "doc.md"
    document.write_text("text", encoding="utf-8")
    client = _vector_client()
    s3 = MagicMock()
    s3.put_object.side_effect = error
    client._s3_client = s3

    with pytest.raises(VectorStoreError, match="upload of doc.md failed"):
        client.ingest([document])


@pytest.mark.parametrize(
    ("error", "exception"),
    [
        (_client_error("ResourceNotFoundException"), VectorStoreUnavailableError),
        (_client_error("AccessDeniedException"), VectorStoreError),
        (EndpointConnectionError(endpoint_url="https://dynamodb"), VectorStoreUnavailableError),
    ],
)
def test_vector_ingest_wait_translates_scan_failures(
    error: Exception, exception: type[Exception]
) -> None:
    client = _vector_client()
    client._dynamodb_client = MagicMock()
    client._dynamodb_client.scan.side_effect = error

    with pytest.raises(exception):
        client._wait_for_sources(["corpus/doc.md"], 1)


def test_checkout_root_rejects_missing_markers(tmp_path: Path) -> None:
    assert vector_store._is_gco_checkout_root(tmp_path) is False


def test_checkout_root_degrades_when_git_cannot_launch(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()
    for name in ("cdk.json", "app.py", "pyproject.toml"):
        (tmp_path / name).write_text("", encoding="utf-8")
    with patch("cli.vector_store.subprocess.run", side_effect=OSError("git missing")):
        assert vector_store._is_gco_checkout_root(tmp_path) is False


def test_checkout_root_rejects_failed_or_wrong_git_top_level(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()
    for name in ("cdk.json", "app.py", "pyproject.toml"):
        (tmp_path / name).write_text("", encoding="utf-8")
    with patch(
        "cli.vector_store.subprocess.run",
        return_value=SimpleNamespace(returncode=1, stdout=""),
    ):
        assert vector_store._is_gco_checkout_root(tmp_path) is False


def test_demo_corpus_requires_at_least_one_markdown_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "docs").mkdir()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(vector_store, "_is_gco_checkout_root", lambda _root: True)

    with pytest.raises(VectorStoreError, match=r"no \*\.md files"):
        vector_store.demo_corpus_paths()


def test_managed_region_shape_helpers_reject_nonobjects() -> None:
    document = {"context": {"deployment_regions": []}}
    with (
        patch("cli.managed_config.validated_regional_deployment_regions"),
        patch("cli.managed_config.validated_deployment_partition"),
        pytest.raises(ValueError, match="must be a JSON object"),
    ):
        managed_config._validate_regional_result(document, ("us-east-1",))
    with pytest.raises(ValueError, match="must be a JSON object"):
        managed_config._effective_deployment_scalars(document)
    with pytest.raises(ValueError, match="must be a JSON object"):
        managed_config._effective_regional(document)


def test_effective_regional_rejects_nonarray() -> None:
    with pytest.raises(ValueError, match="must be a JSON array"):
        managed_config._effective_regional(
            {"context": {"deployment_regions": {"regional": "us-east-1"}}}
        )


@pytest.mark.parametrize(
    "document",
    [
        {"context": {}},
        {"context": {"bedrock": "bad"}},
        {"context": {"bedrock": {}}},
    ],
)
def test_codex_reasoning_validator_handles_absent_and_malformed_containers(
    document: dict[str, object],
) -> None:
    if document.get("context", {}).get("bedrock") == "bad":
        with pytest.raises(ValueError, match="context.bedrock must be a JSON object"):
            managed_config._codex_reasoning_effort_validator(document, "high")
    else:
        managed_config._codex_reasoning_effort_validator(document, "high")


def test_config_mutation_lock_translates_shared_lock_failure(tmp_path: Path) -> None:
    @contextmanager
    def fail_lock(_path: Path):
        raise ConfigMutationLockError("busy")
        yield

    with (
        patch("cli.managed_config._shared_config_mutation_lock", fail_lock),
        pytest.raises(ManagedConfigError, match="busy"),
        managed_config._config_mutation_lock(tmp_path / "cdk.json"),
    ):
        pass


def test_current_managed_list_rejects_nonarray() -> None:
    document = {"context": {"deployment_regions": {"regional": "us-east-1"}}}
    with pytest.raises(ManagedConfigError, match="must be a JSON array"):
        managed_config._current_values(document, REGIONAL_DEPLOYMENT_REGIONS)


def test_scalar_container_rejects_outer_and_nested_nonobjects() -> None:
    with pytest.raises(ManagedConfigError, match="context.bedrock must be a JSON object"):
        managed_config._scalar_container(
            {"context": {"bedrock": "bad"}},
            CODEX_REASONING_EFFORT,
        )
    with pytest.raises(ManagedConfigError, match="context.bedrock.codex must be a JSON object"):
        managed_config._scalar_container(
            {"context": {"bedrock": {"codex": "bad"}}},
            CODEX_REASONING_EFFORT,
        )


def test_current_scalar_rejects_nonstring_leaf() -> None:
    with pytest.raises(ManagedConfigError, match="must be a JSON string"):
        managed_config._current_scalar(
            {"context": {"bedrock": {"codex": {"reasoning_effort": 1}}}},
            CODEX_REASONING_EFFORT,
        )


def test_deployment_region_status_rejects_nonobject_container(tmp_path: Path) -> None:
    config = tmp_path / "cdk.json"
    config.write_text(json.dumps({"context": {"deployment_regions": []}}), encoding="utf-8")
    with pytest.raises(ManagedConfigError, match="must be a JSON object"):
        managed_config.get_deployment_regions_status(config_path=config)


def test_safe_aws_error_message_handles_missing_message() -> None:
    assert aws_client._safe_aws_error_message({}) == "AWS did not return an error message"


def test_execute_api_hostname_rejects_missing_metadata() -> None:
    resolver = MagicMock()
    resolver.construct_endpoint.return_value = None
    session = MagicMock()
    session.get_component.return_value = resolver
    with (
        patch("cli.aws_client.get_botocore_session", return_value=session),
        pytest.raises(ValueError, match="metadata is unavailable"),
    ):
        aws_client._execute_api_service_hostname("us-test-1")


def test_regional_endpoint_rejects_invalid_port_syntax() -> None:
    with pytest.raises(ValueError, match="not a valid URL"):
        aws_client._normalize_regional_api_endpoint(
            "https://abc.execute-api.us-east-1.amazonaws.com:notaport/prod",
            "us-east-1",
        )


@pytest.mark.parametrize("response", [None, {"Stacks": []}])
def test_regional_endpoint_discovery_rejects_invalid_stack_shapes(response: object) -> None:
    client = _aws_client()
    cfn = MagicMock()
    cfn.describe_stacks.return_value = response
    client._session.client.return_value = cfn

    with pytest.raises(RegionalApiDiscoveryError):
        client.get_regional_api_endpoint("us-east-1")


def test_global_endpoint_skips_unrelated_output_before_match() -> None:
    client = _aws_client()
    cfn = MagicMock()
    cfn.describe_stacks.return_value = {
        "Stacks": [
            {
                "Outputs": [
                    {"OutputKey": "Other", "OutputValue": "ignored"},
                    {
                        "OutputKey": "ApiEndpoint",
                        "OutputValue": "https://abc.execute-api.us-east-1.amazonaws.com/prod/",
                    },
                ]
            }
        ]
    }
    client._session.client.return_value = cfn

    assert client.get_api_endpoint().api_id == "abc"


def test_stack_discovery_falls_back_when_no_regions_are_configured() -> None:
    client = _aws_client()
    ec2 = MagicMock()
    ec2.describe_regions.return_value = {"Regions": [{"RegionName": "us-west-2"}]}
    client._session.client.return_value = ec2
    client._get_configured_regions = Mock(return_value=[])
    client._probe_regional_stack = Mock(return_value=None)

    assert client.discover_regional_stacks() == {}
    client._probe_regional_stack.assert_called_once_with("us-west-2")


def test_call_api_ignores_none_query_values() -> None:
    client = _aws_client()
    response = MagicMock(ok=True)
    response.json.return_value = {"ok": True}
    client.make_authenticated_request = Mock(return_value=response)

    assert client.call_api("GET", "/path", params={"status": None}) == {"ok": True}
    assert client.make_authenticated_request.call_args.kwargs["path"] == "/path"


def test_call_api_falls_back_to_status_when_error_json_has_no_message() -> None:
    client = _aws_client()
    response = MagicMock(ok=False, status_code=418, reason="Teapot", text="")
    response.json.return_value = {}
    client.make_authenticated_request = Mock(return_value=response)

    with pytest.raises(APIRequestError, match="418 Teapot"):
        client.call_api("GET", "/path")


def test_403_refresh_returns_original_response_when_new_session_has_no_credentials() -> None:
    client = _aws_client()
    endpoint = ApiEndpoint("https://api.example/prod", "us-east-1", "api")
    client._api_endpoint_cache = endpoint
    client._cache_timestamp = __import__("time").time()
    original_session = client._session
    original_session.get_credentials.return_value = MagicMock()
    response = MagicMock(status_code=403)

    with (
        patch("cli.aws_client.requests.request", return_value=response),
        patch("cli.aws_client.SigV4Auth"),
        patch("cli.aws_client.boto3.Session") as session_factory,
    ):
        session_factory.return_value.get_credentials.return_value = None
        assert client.make_authenticated_request("GET", "/health", max_attempts=2) is response

    response.close.assert_not_called()


@pytest.mark.parametrize(
    ("namespace", "payload", "message"),
    [
        (None, {"resources": []}, "500 Error"),
        (None, {"resources": [{"name": "job", "status": "failed", "message": "bad"}]}, "job: bad"),
        (None, {"message": "service message"}, "service message"),
        (None, {}, "500 Error"),
    ],
)
def test_submit_manifests_error_payload_paths(
    namespace: str | None,
    payload: dict[str, object],
    message: str,
) -> None:
    client = _aws_client()
    response = MagicMock(ok=False, status_code=500, reason="Error", text="")
    response.json.return_value = payload
    client.make_authenticated_request = Mock(return_value=response)

    with pytest.raises(RuntimeError, match=message):
        client.submit_manifests([{"kind": "Job"}], namespace=namespace)


def test_aws_job_wrapper_option_paths_and_policy_readback() -> None:
    client = _aws_client()
    response = MagicMock()
    response.json.return_value = {"ok": True}
    client.make_authenticated_request = Mock(return_value=response)

    client.get_jobs(status="running")
    assert (
        client.make_authenticated_request.call_args.kwargs["path"] == "/api/v1/jobs?status=running"
    )
    client.delete_job("job name", "ns/name", expected_uid="uid/value")
    assert "expected_uid=uid%2Fvalue" in client.make_authenticated_request.call_args.kwargs["path"]
    client.get_global_jobs(limit=5)
    assert (
        client.make_authenticated_request.call_args.kwargs["path"] == "/api/v1/global/jobs?limit=5"
    )
    client.bulk_delete_global(dry_run=False)
    assert client.make_authenticated_request.call_args.kwargs["body"] == {"dry_run": False}
    assert client.get_job_validation_policy("us-east-1") == {"ok": True}
    assert client.make_authenticated_request.call_args.kwargs == {
        "method": "GET",
        "path": "/api/v1/policy",
        "target_region": "us-east-1",
    }


def test_regional_alb_lookup_returns_none_without_stack() -> None:
    client = _aws_client()
    client.get_regional_stack = Mock(return_value=None)
    assert client.get_regional_alb_endpoint("us-east-1") is None


@pytest.mark.parametrize(
    ("force", "expected_method"),
    [(False, None), (True, "kill")],
)
def test_windows_tunnel_tree_without_taskkill_preserves_or_kills_root(
    monkeypatch: pytest.MonkeyPatch,
    force: bool,
    expected_method: str | None,
) -> None:
    proc = MagicMock(pid=123)
    monkeypatch.setattr(ssm_tunnel.os, "name", "nt")
    monkeypatch.setattr(ssm_tunnel.shutil, "which", lambda _name: None)

    ssm_tunnel._signal_api_tunnel_tree(proc, force=force, wait_seconds=1)

    if expected_method is None:
        proc.kill.assert_not_called()
    else:
        proc.kill.assert_called_once_with()


def test_windows_taskkill_launch_error_falls_back_on_force(monkeypatch: pytest.MonkeyPatch) -> None:
    proc = MagicMock(pid=123)
    monkeypatch.setattr(ssm_tunnel.os, "name", "nt")
    monkeypatch.setattr(ssm_tunnel.shutil, "which", lambda _name: "taskkill.exe")
    monkeypatch.setattr(ssm_tunnel.subprocess, "run", Mock(side_effect=OSError("failed")))

    ssm_tunnel._signal_api_tunnel_tree(proc, force=True, wait_seconds=1)

    proc.kill.assert_called_once_with()


@pytest.mark.parametrize(("poll_result", "method"), [(1, None), (None, "terminate")])
def test_posix_group_signal_failure_uses_root_fallback_only_if_running(
    monkeypatch: pytest.MonkeyPatch,
    poll_result: int | None,
    method: str | None,
) -> None:
    proc = MagicMock(pid=123)
    proc.poll.return_value = poll_result
    monkeypatch.setattr(ssm_tunnel.os, "name", "posix")
    monkeypatch.setattr(ssm_tunnel.os, "killpg", Mock(side_effect=OSError("gone")))

    ssm_tunnel._signal_api_tunnel_tree(proc, force=False, wait_seconds=1)

    if method is None:
        proc.terminate.assert_not_called()
    else:
        proc.terminate.assert_called_once_with()


def test_stop_tunnel_requires_positive_wait() -> None:
    with pytest.raises(ValueError, match="must be positive"):
        ssm_tunnel.stop_api_tunnel(MagicMock(), wait_seconds=0)


class _Stream:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


def test_stop_tunnel_reports_process_that_survives_forced_wait() -> None:
    stream = _Stream()
    proc = MagicMock(stdout=stream, stderr=None)
    proc.poll.side_effect = [None, None]
    proc.communicate.side_effect = [
        subprocess.TimeoutExpired("aws", 1),
        subprocess.TimeoutExpired("aws", 1),
    ]
    proc.wait.side_effect = subprocess.TimeoutExpired("aws", 1)

    with (
        patch("cli.ssm_tunnel._signal_api_tunnel_tree"),
        pytest.raises(RuntimeError, match="did not exit after forced termination"),
    ):
        ssm_tunnel.stop_api_tunnel(proc, wait_seconds=1)

    assert stream.closed is True
    proc.kill.assert_called_once_with()


def test_stop_tunnel_reports_inherited_pipes_after_wrapper_exit() -> None:
    stdout = _Stream()
    stderr = _Stream()
    proc = MagicMock(stdout=stdout, stderr=stderr)
    proc.poll.return_value = 1
    proc.communicate.side_effect = [
        subprocess.TimeoutExpired("aws", 1),
        subprocess.TimeoutExpired("aws", 1),
    ]

    with (
        patch("cli.ssm_tunnel._signal_api_tunnel_tree"),
        pytest.raises(RuntimeError, match="retained inherited output pipes"),
    ):
        ssm_tunnel.stop_api_tunnel(proc, wait_seconds=1)

    assert stdout.closed and stderr.closed


def test_exited_tunnel_detail_includes_cleanup_failure() -> None:
    proc = MagicMock()
    proc.poll.return_value = 23
    with patch("cli.ssm_tunnel.stop_api_tunnel", side_effect=RuntimeError("stuck")):
        assert ssm_tunnel.exited_api_tunnel_detail(proc) == (
            "exit code 23; process-tree cleanup failed: stuck"
        )


@pytest.mark.parametrize(
    "kwargs",
    [
        {"ready_wait_seconds": -1},
        {"ready_poll_seconds": 0},
        {"connect_timeout_seconds": 0},
    ],
)
def test_start_tunnel_validates_timing_options(kwargs: dict[str, float]) -> None:
    with pytest.raises(ValueError):
        ssm_tunnel.start_api_tunnel(
            "i-0123456789abcdef0",
            "host.example",
            8443,
            "us-east-1",
            **kwargs,
        )


def test_start_tunnel_reports_cleanup_failure_over_primary_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proc = MagicMock()
    monkeypatch.setattr(ssm_tunnel.subprocess, "Popen", lambda *_args, **_kwargs: proc)
    monkeypatch.setattr(
        ssm_tunnel,
        "exited_api_tunnel_detail",
        Mock(side_effect=RuntimeError("primary")),
    )
    monkeypatch.setattr(
        ssm_tunnel,
        "stop_api_tunnel",
        Mock(side_effect=RuntimeError("cleanup")),
    )

    with pytest.raises(RuntimeError, match="cleanup also failed: cleanup"):
        ssm_tunnel.start_api_tunnel(
            "i-0123456789abcdef0",
            "host.example",
            8443,
            "us-east-1",
        )


def test_vector_ssm_transport_error_is_unavailable() -> None:
    client = VectorStoreClient()
    with (
        patch(
            "gco.services.aws_ssm.get_ssm_parameter",
            side_effect=EndpointConnectionError(endpoint_url="https://ssm"),
        ),
        pytest.raises(VectorStoreUnavailableError, match="SSM unreachable"),
    ):
        client._resolve_ssm_name("vector-store/table-name")


def test_codex_reasoning_validator_rejects_nonobject_nested_codex() -> None:
    with pytest.raises(ValueError, match="context.bedrock.codex must be a JSON object"):
        managed_config._codex_reasoning_effort_validator(
            {"context": {"bedrock": {"codex": "bad"}}},
            "high",
        )


def test_managed_value_helpers_return_defaults_for_absent_leaves() -> None:
    document = {"context": {"deployment_regions": {}, "bedrock": {"codex": {}}}}

    assert managed_config._current_values(document, REGIONAL_DEPLOYMENT_REGIONS) == (
        REGIONAL_DEPLOYMENT_REGIONS.default
    )
    assert managed_config._current_scalar(document, CODEX_REASONING_EFFORT) == (
        CODEX_REASONING_EFFORT.default
    )


def test_scalar_container_returns_none_for_absent_nested_object_without_materializing() -> None:
    document = {"context": {"bedrock": {}}}

    assert managed_config._scalar_container(document, CODEX_REASONING_EFFORT) is None
    assert document == {"context": {"bedrock": {}}}


def test_submit_manifests_uses_error_key_and_nonjson_text_fallback() -> None:
    client = _aws_client()
    error_response = MagicMock(ok=False, status_code=500, reason="Error", text="")
    error_response.json.return_value = {"error": "explicit error"}
    client.make_authenticated_request = Mock(return_value=error_response)
    with pytest.raises(RuntimeError, match="explicit error"):
        client.submit_manifests([{"kind": "Job"}])

    text_response = MagicMock(ok=False, status_code=500, reason="Error", text="plain text")
    text_response.json.side_effect = json.JSONDecodeError("bad", "", 0)
    client.make_authenticated_request = Mock(return_value=text_response)
    with pytest.raises(RuntimeError, match="plain text"):
        client.submit_manifests([{"kind": "Job"}])


def test_delete_job_without_uid_omits_query_string() -> None:
    client = _aws_client()
    response = MagicMock()
    response.json.return_value = {"deleted": True}
    client.make_authenticated_request = Mock(return_value=response)

    client.delete_job("job", "namespace")

    assert client.make_authenticated_request.call_args.kwargs["path"] == (
        "/api/v1/jobs/namespace/job"
    )
