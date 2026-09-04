"""Behavioral gap coverage for inference services, CLI adapters, and MCP tools.

The baseline report leaves mostly defensive and concurrency branches in these
modules.  Tests here exercise those contracts with DynamoDB, network, AWS, and
MCP dependencies replaced by local doubles; no external service is contacted.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import runpy
import sys
import types
import warnings
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, Mock, call, patch

import pytest
from botocore.exceptions import ClientError
from click.testing import CliRunner
from fastapi import Request

from cli.config import GCOConfig
from cli.inference import InferenceManager
from cli.main import cli
from gco.inference_proxy_config import compute_inference_proxy_tls_replacements
from gco.models.inference_models import InferenceEndpointSpec
from gco.services import inference_api
from gco.services.inference_store import InferenceEndpointStore, _validate_endpoint_spec
from gco.services.tls_proxy import (
    TLS_CERT_FILE_ENV,
    TLS_KEY_FILE_ENV,
    ProxyConfig,
    TlsProxy,
    _non_negative_number,
    _positive_port,
    load_proxy_config,
    run_proxy,
)

# ---------------------------------------------------------------------------
# Small model/config/API surfaces
# ---------------------------------------------------------------------------


def test_endpoint_spec_serializes_model_source_and_autoscaling() -> None:
    spec = InferenceEndpointSpec(
        image="registry.example/model:v1",
        model_source="s3://model-bucket/model/",
        autoscaling={"enabled": True, "min_replicas": 2, "max_replicas": 8},
    )

    assert spec.to_dict()["model_source"] == "s3://model-bucket/model/"
    assert spec.to_dict()["autoscaling"] == {
        "enabled": True,
        "min_replicas": 2,
        "max_replicas": 8,
    }


@pytest.mark.parametrize(
    "config",
    [
        {
            "tls_proxy_cpu_request_millicores": "100",
            "tls_proxy_cpu_target_utilization_percentage": 70,
        },
        {
            "tls_proxy_cpu_request_millicores": 100,
            "tls_proxy_cpu_target_utilization_percentage": True,
        },
    ],
)
def test_inference_proxy_replacements_reject_non_plain_integers(config: dict[str, object]) -> None:
    with pytest.raises(ValueError, match="must be integers"):
        compute_inference_proxy_tls_replacements(config)


@pytest.mark.asyncio
async def test_inference_api_handlers_return_stable_contracts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("REGION", "eu-west-1")

    assert await inference_api.root() == {
        "service": "GCO Inference Proxy API",
        "version": "1.0.0",
        "status": "running",
        "region": "eu-west-1",
    }
    assert await inference_api.kubernetes_health_check() == {"status": "ok"}
    assert await inference_api.kubernetes_readiness_check() == {"status": "ready"}
    assert inference_api.create_app() is inference_api.app


@pytest.mark.asyncio
async def test_inference_api_exception_handler_hides_internal_details() -> None:
    request = MagicMock(spec=Request)
    request.method = "POST"
    request.url.path = "/inference/private-model"

    response = await inference_api.global_exception_handler(request, RuntimeError("secret route"))

    assert response.status_code == 500
    assert json.loads(response.body) == {"error": "Internal server error"}
    assert b"secret route" not in response.body


# ---------------------------------------------------------------------------
# DynamoDB store defensive and conditional-write behavior
# ---------------------------------------------------------------------------


def _client_error(code: str) -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": "test failure"}}, "operation")


@pytest.fixture
def endpoint_store() -> tuple[InferenceEndpointStore, MagicMock]:
    table = MagicMock()
    store = object.__new__(InferenceEndpointStore)
    store.table_name = "test-table"
    store._region = "us-east-1"
    store._table = table
    return store, table


def test_store_rejects_non_mapping_specs_before_persistence() -> None:
    with pytest.raises(ValueError, match="must be a mapping"):
        _validate_endpoint_spec(["not", "a", "mapping"])  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("endpoint", "message"),
    [
        ({"updated_at": "snapshot"}, "endpoint name"),
        ({"endpoint_name": "legacy"}, "migration timestamp"),
    ],
)
def test_lifecycle_migration_requires_identity_and_snapshot(
    endpoint_store: tuple[InferenceEndpointStore, MagicMock],
    endpoint: dict[str, Any],
    message: str,
) -> None:
    store, table = endpoint_store

    with pytest.raises(ValueError, match=message):
        store.ensure_lifecycle_metadata(endpoint)

    table.update_item.assert_not_called()


@pytest.mark.parametrize(
    ("code", "expected"),
    [("ConditionalCheckFailedException", None), ("InternalServerError", "raises")],
)
def test_lifecycle_migration_distinguishes_races_from_service_failures(
    endpoint_store: tuple[InferenceEndpointStore, MagicMock],
    code: str,
    expected: str | None,
) -> None:
    store, table = endpoint_store
    table.update_item.side_effect = _client_error(code)
    snapshot = {
        "endpoint_name": "legacy",
        "updated_at": "snapshot",
        "target_regions": ["us-east-1"],
    }

    if expected == "raises":
        with pytest.raises(ClientError):
            store.ensure_lifecycle_metadata(snapshot)
    else:
        assert store.ensure_lifecycle_metadata(snapshot) is None


@pytest.mark.parametrize(
    ("label", "lifecycle", "message"),
    [
        (("", "value"), None, "label name and value"),
        (("team", ""), None, "label name and value"),
        (None, "", "lifecycle id"),
    ],
)
def test_conditioned_identity_rejects_ambiguous_predicates(
    label: tuple[str, str] | None,
    lifecycle: str | None,
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        InferenceEndpointStore._conditioned_identity(label, lifecycle)


def test_conditioned_identity_allows_unconditioned_existing_item_check() -> None:
    condition, names, values = InferenceEndpointStore._conditioned_identity(None, None)
    assert condition == "attribute_exists(endpoint_name)"
    assert names == {}
    assert values == {}


@pytest.mark.parametrize(
    ("operation", "invoke"),
    [
        (
            "desired state",
            lambda store: store.update_desired_state(
                "ep", "running", expected_lifecycle_id="life-1"
            ),
        ),
        (
            "spec",
            lambda store: store.update_spec("ep", {"image": "v2"}, expected_lifecycle_id="life-1"),
        ),
        (
            "target regions",
            lambda store: store.update_target_regions(
                "ep",
                ["us-east-1", "eu-west-1"],
                ["us-east-1", "eu-west-1"],
                {"us-east-1": "east", "eu-west-1": "west"},
                expected_lifecycle_id="life-1",
                expected_updated_at="snapshot",
            ),
        ),
        (
            "delete",
            lambda store: store.delete_endpoint("ep", expected_lifecycle_id="life-1"),
        ),
        (
            "scale",
            lambda store: store.scale_endpoint("ep", 3, expected_lifecycle_id="life-1"),
        ),
    ],
)
def test_store_propagates_nonconditional_dynamodb_failures(
    endpoint_store: tuple[InferenceEndpointStore, MagicMock],
    operation: str,
    invoke: Any,
) -> None:
    del operation
    store, table = endpoint_store
    table.get_item.return_value = {
        "Item": {
            "endpoint_name": "ep",
            "lifecycle_id": "life-1",
            "updated_at": "snapshot",
            "desired_state": "running",
            "target_regions": ["us-east-1"],
            "cleanup_regions": ["us-east-1"],
            "region_generations": {"us-east-1": "east"},
        }
    }
    table.update_item.side_effect = _client_error("InternalServerError")
    table.delete_item.side_effect = _client_error("InternalServerError")

    with pytest.raises(ClientError):
        invoke(store)


@pytest.mark.parametrize(
    ("method", "message"),
    [
        (lambda store: store.update_spec("ep", {}, expected_lifecycle_id=""), "Spec updates"),
        (lambda store: store.scale_endpoint("ep", 2, expected_lifecycle_id=""), "Scaling"),
    ],
)
def test_store_mutations_require_lifecycle_identity(
    endpoint_store: tuple[InferenceEndpointStore, MagicMock],
    method: Any,
    message: str,
) -> None:
    store, table = endpoint_store
    with pytest.raises(ValueError, match=message):
        method(store)
    table.update_item.assert_not_called()


@pytest.mark.parametrize(
    ("current", "migration", "message"),
    [
        (None, MagicMock(), None),
        ({"endpoint_name": "ep", "updated_at": "snapshot"}, None, None),
        (
            {
                "endpoint_name": "ep",
                "updated_at": "snapshot",
                "desired_state": "stopped",
            },
            {"endpoint_name": "ep", "desired_state": "stopped"},
            "lifecycle identity",
        ),
        (
            {
                "endpoint_name": "ep",
                "updated_at": "snapshot",
                "desired_state": "running",
                "lifecycle_id": "life-1",
                "cleanup_regions": [],
                "region_generations": {},
            },
            MagicMock(),
            "only stopped",
        ),
    ],
)
def test_start_endpoint_handles_absence_migration_races_and_invalid_state(
    endpoint_store: tuple[InferenceEndpointStore, MagicMock],
    current: dict[str, Any] | None,
    migration: Any,
    message: str | None,
) -> None:
    store, _table = endpoint_store
    store.get_endpoint = MagicMock(return_value=current)  # type: ignore[method-assign]
    migrated = current if message == "only stopped" else migration
    store.ensure_lifecycle_metadata = MagicMock(return_value=migrated)  # type: ignore[method-assign]

    if message:
        with pytest.raises(ValueError, match=message):
            store.start_endpoint("ep")
    else:
        assert store.start_endpoint("ep") is None


@pytest.mark.parametrize(
    "current",
    [
        None,
        {"lifecycle_id": "other", "updated_at": "snapshot", "desired_state": "running"},
        {"lifecycle_id": "life-1", "updated_at": "other", "desired_state": "running"},
        {"lifecycle_id": "life-1", "updated_at": "snapshot", "desired_state": "deleted"},
    ],
)
def test_target_region_update_rejects_stale_snapshots_before_write(
    endpoint_store: tuple[InferenceEndpointStore, MagicMock], current: dict[str, Any] | None
) -> None:
    store, table = endpoint_store
    store.get_endpoint = MagicMock(return_value=current)  # type: ignore[method-assign]

    assert (
        store.update_target_regions(
            "ep",
            ["us-east-1"],
            ["us-east-1"],
            {},
            expected_lifecycle_id="life-1",
            expected_updated_at="snapshot",
        )
        is None
    )
    table.update_item.assert_not_called()


def test_target_region_update_generates_missing_cleanup_tokens(
    endpoint_store: tuple[InferenceEndpointStore, MagicMock],
) -> None:
    store, table = endpoint_store
    store.get_endpoint = MagicMock(  # type: ignore[method-assign]
        return_value={
            "lifecycle_id": "life-1",
            "updated_at": "snapshot",
            "desired_state": "running",
            "target_regions": ["us-east-1"],
            "cleanup_regions": ["us-east-1", "eu-west-1"],
            "region_generations": {"us-east-1": "east", "eu-west-1": ""},
        }
    )
    table.update_item.return_value = {"Attributes": {"endpoint_name": "ep"}}

    result = store.update_target_regions(
        "ep",
        ["us-east-1"],
        ["us-east-1", "eu-west-1"],
        {},
        expected_lifecycle_id="life-1",
        expected_updated_at="snapshot",
    )

    assert result == {"endpoint_name": "ep"}
    generations = table.update_item.call_args.kwargs["ExpressionAttributeValues"][
        ":region_generations"
    ]
    assert generations["us-east-1"] == "east"
    assert len(generations["eu-west-1"]) == 64


@pytest.mark.parametrize(
    ("code", "expected"),
    [("ConditionalCheckFailedException", False), ("InternalServerError", False)],
)
def test_region_status_returns_false_for_stale_and_service_failures(
    endpoint_store: tuple[InferenceEndpointStore, MagicMock],
    code: str,
    expected: bool,
) -> None:
    store, table = endpoint_store
    table.update_item.side_effect = _client_error(code)

    assert (
        store.update_region_status(
            "ep",
            "us-east-1",
            "deleting",
            expected_lifecycle_id="life-1",
            expected_deletion_generation="delete-1",
        )
        is expected
    )


# ---------------------------------------------------------------------------
# TLS proxy validation, network failure, rotation, drain, and orchestration
# ---------------------------------------------------------------------------


def _proxy_config(tmp_path: Path) -> ProxyConfig:
    return ProxyConfig(
        host="127.0.0.1",
        port=8443,
        upstream_host="127.0.0.1",
        upstream_port=9000,
        cert_file=tmp_path / "tls.crt",
        key_file=tmp_path / "tls.key",
        poll_seconds=0,
        graceful_shutdown_seconds=0,
    )


@pytest.mark.parametrize("value", ["0", "65536"])
def test_tls_port_validation_rejects_out_of_range_values(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv("TEST_TLS_PORT", value)
    with pytest.raises(RuntimeError, match="between 1 and 65535"):
        _positive_port("TEST_TLS_PORT", 8443)


@pytest.mark.parametrize("value", ["-0.1", "nan", "inf", "-inf"])
def test_tls_duration_validation_rejects_negative_or_nonfinite_values(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv("TEST_TLS_DURATION", value)
    with pytest.raises(RuntimeError, match="finite non-negative"):
        _non_negative_number("TEST_TLS_DURATION", 1.0)


@pytest.mark.parametrize("empty_variable", [TLS_CERT_FILE_ENV, TLS_KEY_FILE_ENV])
def test_tls_config_rejects_empty_projected_keypair_paths(
    monkeypatch: pytest.MonkeyPatch, empty_variable: str
) -> None:
    monkeypatch.setenv(TLS_CERT_FILE_ENV, "/tls/cert.pem")
    monkeypatch.setenv(TLS_KEY_FILE_ENV, "/tls/key.pem")
    monkeypatch.setenv(empty_variable, "   ")

    with pytest.raises(RuntimeError, match="must not be empty"):
        load_proxy_config()


@pytest.mark.asyncio
async def test_tls_connection_failure_closes_client_without_leaking_task(tmp_path: Path) -> None:
    proxy = TlsProxy(_proxy_config(tmp_path))
    client_writer = MagicMock()
    client_writer.wait_closed = AsyncMock()

    with patch(
        "gco.services.tls_proxy.asyncio.open_connection", AsyncMock(side_effect=OSError("down"))
    ):
        await proxy._handle_connection(MagicMock(), client_writer)

    client_writer.close.assert_called_once_with()
    client_writer.wait_closed.assert_awaited_once_with()
    assert proxy._connections == set()


@pytest.mark.asyncio
async def test_tls_connection_without_current_task_still_closes_client(tmp_path: Path) -> None:
    proxy = TlsProxy(_proxy_config(tmp_path))
    client_writer = MagicMock()
    client_writer.wait_closed = AsyncMock()

    with (
        patch("gco.services.tls_proxy.asyncio.current_task", return_value=None),
        patch(
            "gco.services.tls_proxy.asyncio.open_connection",
            AsyncMock(side_effect=ConnectionError("refused")),
        ),
    ):
        await proxy._handle_connection(MagicMock(), client_writer)

    client_writer.close.assert_called_once_with()
    assert proxy._connections == set()


@pytest.mark.asyncio
async def test_tls_connection_cancels_opposite_pump_and_closes_both_writers(tmp_path: Path) -> None:
    proxy = TlsProxy(_proxy_config(tmp_path))
    client_reader = MagicMock()
    client_reader.read = AsyncMock(return_value=b"")
    upstream_reader = MagicMock()
    blocker = asyncio.Event()

    async def blocked_read(_size: int) -> bytes:
        await blocker.wait()
        return b""

    upstream_reader.read = AsyncMock(side_effect=blocked_read)
    client_writer = MagicMock()
    client_writer.wait_closed = AsyncMock()
    upstream_writer = MagicMock()
    upstream_writer.wait_closed = AsyncMock()

    with patch(
        "gco.services.tls_proxy.asyncio.open_connection",
        AsyncMock(return_value=(upstream_reader, upstream_writer)),
    ):
        await proxy._handle_connection(client_reader, client_writer)

    upstream_writer.close.assert_called_once_with()
    client_writer.close.assert_called_once_with()
    assert proxy._connections == set()


@pytest.mark.asyncio
async def test_tls_reload_without_existing_acceptor_starts_replacement(tmp_path: Path) -> None:
    proxy = TlsProxy(_proxy_config(tmp_path))
    replacement = MagicMock()

    with patch(
        "gco.services.tls_proxy.asyncio.start_server", AsyncMock(return_value=replacement)
    ) as start_server:
        await proxy._reload_certificate(MagicMock(), "digest-v2")

    start_server.assert_awaited_once()
    assert proxy._server is replacement
    assert proxy._keypair_digest == "digest-v2"


@pytest.mark.asyncio
async def test_tls_watcher_exits_immediately_after_stop(tmp_path: Path) -> None:
    proxy = TlsProxy(_proxy_config(tmp_path))
    proxy._stop.set()
    with patch("gco.services.tls_proxy._ssl_context") as ssl_context:
        await proxy.watch_certificates()
    ssl_context.assert_not_called()


@pytest.mark.asyncio
async def test_tls_watcher_rejects_bad_rotated_keypair_then_stops(tmp_path: Path) -> None:
    proxy = TlsProxy(_proxy_config(tmp_path))

    async def expire(awaitable: Any, *, timeout: float) -> Any:
        del timeout
        awaitable.close()
        raise TimeoutError

    def reject(_config: ProxyConfig) -> tuple[Any, str]:
        proxy._stop.set()
        raise RuntimeError("partial projected secret")

    with (
        patch("gco.services.tls_proxy.asyncio.wait_for", side_effect=expire),
        patch("gco.services.tls_proxy._ssl_context", side_effect=reject),
    ):
        await proxy.watch_certificates()

    assert proxy._stop.is_set()


@pytest.mark.asyncio
async def test_tls_shutdown_without_server_or_streams_is_idempotent(tmp_path: Path) -> None:
    proxy = TlsProxy(_proxy_config(tmp_path))
    await proxy.shutdown()
    await proxy.shutdown()
    assert proxy._stop.is_set()


@pytest.mark.asyncio
async def test_tls_shutdown_cancels_stream_after_zero_drain_budget(tmp_path: Path) -> None:
    proxy = TlsProxy(_proxy_config(tmp_path))
    server = MagicMock()
    server.wait_closed = AsyncMock()
    proxy._server = server

    async def hanging_stream() -> None:
        await asyncio.Event().wait()

    active = asyncio.create_task(hanging_stream())
    proxy._connections.add(active)

    await proxy.shutdown()

    assert active.cancelled()
    server.close.assert_called_once_with()
    server.wait_closed.assert_awaited_once_with()


class _FakeRunProxy:
    def __init__(self, config: ProxyConfig, *, watcher_error: Exception | None = None) -> None:
        self.config = config
        self._stop = asyncio.Event()
        self.watcher_error = watcher_error
        self.started = False
        self.shutdown_called = False

    async def start(self) -> None:
        self.started = True
        if self.watcher_error is None:
            self._stop.set()

    async def watch_certificates(self) -> None:
        if self.watcher_error is not None:
            raise self.watcher_error
        # Keep the watcher pending so the stop waiter wins the orchestration race.
        await asyncio.Event().wait()

    async def shutdown(self) -> None:
        self.shutdown_called = True


@pytest.mark.asyncio
@pytest.mark.parametrize("watcher_error", [None, RuntimeError("watcher failed")])
async def test_run_proxy_drains_on_stop_and_propagates_watcher_failure(
    tmp_path: Path, watcher_error: Exception | None
) -> None:
    config = _proxy_config(tmp_path)
    fake = _FakeRunProxy(config, watcher_error=watcher_error)
    loop = MagicMock()
    loop.add_signal_handler.side_effect = [NotImplementedError, None]

    with (
        patch("gco.services.tls_proxy.TlsProxy", return_value=fake),
        patch("gco.services.tls_proxy.asyncio.get_running_loop", return_value=loop),
    ):
        if watcher_error is None:
            await run_proxy(config)
        else:
            with pytest.raises(RuntimeError, match="watcher failed"):
                await run_proxy(config)

    assert fake.started is True
    assert fake.shutdown_called is True
    assert loop.add_signal_handler.call_count == 2


def test_tls_main_and_module_entry_run_async_proxy() -> None:
    with patch("gco.services.tls_proxy.asyncio.run") as async_run:
        from gco.services.tls_proxy import main

        main()
        assert async_run.call_count == 1
        async_run.call_args.args[0].close()

    with patch("asyncio.run") as module_run, warnings.catch_warnings():
        warnings.filterwarnings(
            "ignore",
            message=".*gco.services.tls_proxy.*found in sys.modules.*",
            category=RuntimeWarning,
        )
        runpy.run_module("gco.services.tls_proxy", run_name="__main__")
        assert module_run.call_count == 1
        module_run.call_args.args[0].close()


# ---------------------------------------------------------------------------
# InferenceManager lifecycle/error behavior, including conflict regressions
# ---------------------------------------------------------------------------


def _manager_with_store(store: MagicMock) -> InferenceManager:
    manager = InferenceManager.__new__(InferenceManager)
    manager.config = SimpleNamespace(global_region="us-west-2")
    manager._aws_client = MagicMock()
    manager._get_store = MagicMock(return_value=store)  # type: ignore[method-assign]
    return manager


def _mutable_endpoint(spec: Any = None, **updates: Any) -> dict[str, Any]:
    endpoint: dict[str, Any] = {
        "endpoint_name": "ep",
        "desired_state": "running",
        "lifecycle_id": "life-1",
        "updated_at": "snapshot",
        "target_regions": ["us-east-1"],
        "cleanup_regions": ["us-east-1"],
        "region_generations": {"us-east-1": "east"},
        "spec": {"image": "old:v1"} if spec is None else spec,
    }
    endpoint.update(updates)
    return endpoint


def test_manager_get_store_uses_explicit_or_global_region() -> None:
    manager = InferenceManager.__new__(InferenceManager)
    manager.config = SimpleNamespace(global_region="us-west-2")
    manager._aws_client = MagicMock()

    with patch("gco.services.inference_store.InferenceEndpointStore") as store_class:
        assert manager._get_store("eu-central-1") is store_class.return_value
        assert manager._get_store() is store_class.return_value

    assert store_class.call_args_list == [
        call(region="eu-central-1"),
        call(region="us-west-2"),
    ]


def test_manager_deploy_uses_default_disaggregated_image() -> None:
    store = MagicMock()
    store.create_endpoint.return_value = {"endpoint_name": "ep"}
    manager = _manager_with_store(store)

    with patch(
        "cli.images.default_disaggregated_image", return_value="vllm/mooncake:default"
    ) as default_image:
        manager.deploy(
            "ep",
            image=None,
            target_regions=["us-east-1"],
            mooncake_mode="store",
        )

    default_image.assert_called_once_with(config=manager.config)
    spec = store.create_endpoint.call_args.kwargs["spec"]
    assert spec["image"] == "vllm/mooncake:default"
    assert spec["framework"] == "vllm"


@pytest.mark.parametrize(
    ("migration", "message"),
    [(None, "changed while initializing"), ({"endpoint_name": "ep"}, "no lifecycle identity")],
)
def test_manager_mutable_load_rejects_failed_or_incomplete_migration(
    migration: dict[str, Any] | None, message: str
) -> None:
    store = MagicMock()
    store.get_endpoint.return_value = {"endpoint_name": "ep", "updated_at": "snapshot"}
    store.ensure_lifecycle_metadata.return_value = migration

    with pytest.raises(ValueError, match=message):
        InferenceManager._load_mutable_endpoint(store, "ep", "updated")


@pytest.mark.parametrize(
    ("operation", "endpoint", "invoke"),
    [
        (
            "scaled",
            _mutable_endpoint(),
            lambda manager: manager.scale("ep", 2),
        ),
        (
            "updated",
            _mutable_endpoint({"image": "v1", "mooncake": {"mode": "disaggregated"}}),
            lambda manager: manager.set_topology("ep", 2, 3),
        ),
        (
            "configured",
            _mutable_endpoint(
                {
                    "image": "v1",
                    "mooncake": {"mode": "store", "store": {"enabled": True}},
                }
            ),
            lambda manager: manager.configure_store("ep", {"enabled": True}),
        ),
        (
            "stopped",
            _mutable_endpoint(),
            lambda manager: manager.stop("ep"),
        ),
        (
            "updated",
            _mutable_endpoint(),
            lambda manager: manager.update_image("ep", "new:v2"),
        ),
        (
            "updated",
            _mutable_endpoint(),
            lambda manager: manager.canary_deploy("ep", "canary:v2"),
        ),
        (
            "updated",
            _mutable_endpoint({"image": "v1", "canary": {"image": "canary:v2", "weight": 10}}),
            lambda manager: manager.promote_canary("ep"),
        ),
        (
            "updated",
            _mutable_endpoint({"image": "v1", "canary": {"image": "canary:v2", "weight": 10}}),
            lambda manager: manager.rollback_canary("ep"),
        ),
    ],
)
def test_manager_surfaces_optimistic_write_conflicts(
    operation: str,
    endpoint: dict[str, Any],
    invoke: Any,
) -> None:
    store = MagicMock()
    store.get_endpoint.return_value = endpoint
    store.scale_endpoint.return_value = None
    store.update_spec.return_value = None
    store.update_desired_state.return_value = None
    manager = _manager_with_store(store)

    with pytest.raises(ValueError, match=f"changed while being {operation}"):
        invoke(manager)


@pytest.mark.parametrize(
    ("method_name", "region", "operation"),
    [
        ("add_region", "eu-west-1", "added to a Region"),
        ("remove_region", "us-east-1", "removed from a Region"),
    ],
)
def test_region_membership_conflict_is_not_misreported_as_not_found(
    method_name: str, region: str, operation: str
) -> None:
    store = MagicMock()
    store.get_endpoint.return_value = _mutable_endpoint()
    store.update_target_regions.return_value = None
    manager = _manager_with_store(store)

    with pytest.raises(ValueError, match=f"changed while being {operation}"):
        getattr(manager, method_name)("ep", region)


@pytest.mark.parametrize("method_name", ["add_region", "remove_region"])
def test_region_membership_requires_conditional_timestamp(method_name: str) -> None:
    endpoint = _mutable_endpoint()
    endpoint.pop("updated_at")
    store = MagicMock()
    store.get_endpoint.return_value = endpoint
    manager = _manager_with_store(store)

    with pytest.raises(ValueError, match="no conditional update timestamp"):
        getattr(manager, method_name)("ep", "eu-west-1")


@pytest.mark.parametrize(
    ("method_name", "args", "endpoint"),
    [
        ("set_topology", (2, 3), _mutable_endpoint(["invalid"])),
        ("configure_store", ({"enabled": True},), _mutable_endpoint(["invalid"])),
        ("update_image", ("new:v2",), _mutable_endpoint(["invalid"])),
        ("canary_deploy", ("canary:v2",), _mutable_endpoint(["invalid"])),
        ("promote_canary", (), _mutable_endpoint(["invalid"])),
        ("rollback_canary", (), _mutable_endpoint(["invalid"])),
    ],
)
def test_manager_rejects_malformed_persisted_specs(
    method_name: str, args: tuple[Any, ...], endpoint: dict[str, Any]
) -> None:
    store = MagicMock()
    store.get_endpoint.return_value = endpoint
    manager = _manager_with_store(store)

    with pytest.raises(ValueError, match="invalid spec"):
        getattr(manager, method_name)("ep", *args)


def test_manager_delete_handles_absence_migration_race_and_missing_lifecycle() -> None:
    store = MagicMock()
    manager = _manager_with_store(store)

    store.get_endpoint.return_value = None
    assert manager.delete("ep") is None

    store.get_endpoint.return_value = {"endpoint_name": "ep"}
    store.ensure_lifecycle_metadata.return_value = None
    with pytest.raises(ValueError, match="initializing deletion identity"):
        manager.delete("ep")

    store.ensure_lifecycle_metadata.return_value = {"endpoint_name": "ep"}
    with pytest.raises(ValueError, match="no lifecycle identity"):
        manager.delete("ep")


def test_manager_owner_condition_requires_lifecycle() -> None:
    manager = _manager_with_store(MagicMock())
    with pytest.raises(ValueError, match="also requires"):
        manager.delete("ep", expected_owner_label=("team", "ml"))


# ---------------------------------------------------------------------------
# Click inference command edge behavior
# ---------------------------------------------------------------------------


@contextmanager
def _cli_config() -> Iterator[GCOConfig]:
    config = GCOConfig()
    with patch("cli.main.get_config", return_value=config):
        yield config


def _invoke_cli(
    args: list[str],
    *,
    manager: MagicMock | None = None,
    aws_client: MagicMock | None = None,
    input_text: str | None = None,
) -> Any:
    manager = manager or MagicMock()
    aws_client = aws_client or MagicMock()
    with (
        _cli_config(),
        patch("cli.inference.get_inference_manager", return_value=manager),
        patch("cli.aws_client.get_aws_client", return_value=aws_client),
    ):
        return CliRunner().invoke(cli, args, input=input_text)


def test_cli_deploy_parses_multiple_values_and_warns_for_omitted_regions() -> None:
    manager = MagicMock()
    manager.deploy.return_value = {
        "endpoint_name": "ep",
        "target_regions": ["us-east-1"],
        "ingress_path": "/inference/ep",
    }
    aws_client = MagicMock()
    aws_client.discover_regional_stacks.return_value = {
        "us-east-1": {},
        "eu-west-1": {},
    }

    result = _invoke_cli(
        [
            "inference",
            "deploy",
            "ep",
            "-i",
            "image:v1",
            "-r",
            "us-east-1",
            "-e",
            "A=1",
            "-e",
            "B=two=parts",
            "-l",
            "team=ml",
            "-l",
            "invalid-label",
            "--node-selector",
            "zone=a",
            "--node-selector",
            "invalid-selector",
            "--autoscale-metric",
            "cpu:65",
            "--autoscale-metric",
            "memory",
            "--mooncake-proxy-image",
            "proxy:v2",
            "--mooncake-admin-key-secret",
            "admin-key",
        ],
        manager=manager,
        aws_client=aws_client,
    )

    assert result.exit_code == 0, result.output
    kwargs = manager.deploy.call_args.kwargs
    assert kwargs["env"] == {"A": "1", "B": "two=parts"}
    assert kwargs["labels"] == {"team": "ml"}
    assert kwargs["node_selector"] == {"zone": "a"}
    assert kwargs["autoscaling"]["metrics"] == [
        {"type": "cpu", "target": 65},
        {"type": "memory", "target": 70},
    ]
    assert kwargs["mooncake_proxy"] == {
        "image": "proxy:v2",
        "admin_api_key_secret": "admin-key",
    }
    assert "NOT deployed to: eu-west-1" in result.output


def test_cli_status_renders_multiple_regions_and_short_timestamp_without_truncation() -> None:
    manager = MagicMock()
    manager.get_endpoint.return_value = {
        "endpoint_name": "ep",
        "desired_state": "running",
        "spec": {"image": "image:v1"},
        "region_status": {
            "us-east-1": {"state": "running", "last_sync": "short"},
            "eu-west-1": {"state": "creating", "last_sync": "also-short"},
        },
    }

    result = _invoke_cli(["inference", "status", "ep"], manager=manager)

    assert result.exit_code == 0, result.output
    assert "us-east-1" in result.output and "eu-west-1" in result.output
    assert "also-short" in result.output


def test_cli_stop_confirmation_path_invokes_manager() -> None:
    manager = MagicMock()
    manager.stop.return_value = {"desired_state": "stopped"}

    result = _invoke_cli(["inference", "stop", "ep"], manager=manager, input_text="y\n")

    assert result.exit_code == 0, result.output
    manager.stop.assert_called_once_with("ep")


@pytest.mark.parametrize(
    ("extra_args", "expected_message"),
    [
        (["--expected-owner-label", "missing-equals"], "must be KEY=VALUE"),
        (["--expected-lifecycle-id", "   "], "must be non-empty"),
        (["--expected-owner-label", "team=ml"], "must be supplied together"),
        (["--expected-lifecycle-id", "life-1"], "must be supplied together"),
    ],
)
def test_cli_delete_rejects_ambiguous_automation_conditions(
    extra_args: list[str], expected_message: str
) -> None:
    result = _invoke_cli(["inference", "delete", "ep", "-y", *extra_args])
    assert result.exit_code != 0
    assert expected_message in result.output


def test_cli_delete_reports_conditioned_not_found_distinctly() -> None:
    manager = MagicMock()
    manager.delete.return_value = None

    result = _invoke_cli(
        [
            "inference",
            "delete",
            "ep",
            "-y",
            "--expected-owner-label",
            "team=ml",
            "--expected-lifecycle-id",
            "life-1",
        ],
        manager=manager,
    )

    assert result.exit_code != 0
    assert "ownership/lifecycle condition failed" in result.output


def test_cli_canary_reports_unexpected_backend_failure() -> None:
    manager = MagicMock()
    manager.canary_deploy.side_effect = RuntimeError("DynamoDB unavailable")

    result = _invoke_cli(["inference", "canary", "ep", "-i", "canary:v2"], manager=manager)

    assert result.exit_code != 0
    assert "Failed to start canary: DynamoDB unavailable" in result.output


@pytest.mark.parametrize("command", ["promote", "rollback"])
def test_cli_canary_mutation_can_be_cancelled(command: str) -> None:
    manager = MagicMock()
    manager.get_endpoint.return_value = {
        "spec": {"image": "primary:v1", "canary": {"image": "canary:v2", "weight": 20}}
    }

    result = _invoke_cli(["inference", command, "ep"], manager=manager, input_text="n\n")

    assert result.exit_code == 0, result.output
    assert "Cancelled" in result.output
    getattr(manager, f"{command}_canary").assert_not_called()


def test_cli_models_prints_non_json_success_response() -> None:
    manager = MagicMock()
    manager.get_endpoint.return_value = {
        "ingress_path": "/inference/ep",
        "spec": {"framework": "vllm", "image": "private/image:v1"},
    }
    aws_client = MagicMock()
    response = MagicMock(ok=True, text="model server warming")
    response.json.side_effect = json.JSONDecodeError("bad json", "", 0)
    aws_client.make_authenticated_request.return_value = response

    result = _invoke_cli(["inference", "models", "ep"], manager=manager, aws_client=aws_client)

    assert result.exit_code == 0, result.output
    assert "model server warming" in result.output


def test_cli_configure_store_reports_manager_conflict_as_failure() -> None:
    manager = MagicMock()
    manager.get_endpoint.return_value = {
        "spec": {"mooncake": {"mode": "store", "store": {"enabled": True}}}
    }
    manager.configure_store.return_value = None

    result = _invoke_cli(
        ["inference", "configure-store", "ep", "--offload", "cpu"], manager=manager
    )

    assert result.exit_code != 0
    assert "not found" in result.output


# ---------------------------------------------------------------------------
# Isolated MCP wrapper behavior
# ---------------------------------------------------------------------------


class _McpStub:
    def tool(self, **_kwargs: Any) -> Any:
        return lambda function: function


def _stub_module(name: str, **attrs: Any) -> types.ModuleType:
    module = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(module, key, value)
    return module


@contextmanager
def _isolated_inference_tool() -> Iterator[tuple[types.ModuleType, Mock, Mock]]:
    run_cli = Mock(return_value='{"ok":true}')
    get_context = Mock(side_effect=LookupError("no context"))
    modules = {
        "cli_runner": _stub_module("cli_runner", _run_cli=run_cli),
        "audit": _stub_module("audit", audit_logged=lambda function: function),
        "server": _stub_module("server", mcp=_McpStub()),
        "feature_flags": _stub_module(
            "feature_flags",
            FLAG_DESTRUCTIVE_OPERATIONS="destructive",
            is_enabled=lambda _flag: False,
        ),
        "fastmcp": _stub_module("fastmcp"),
        "fastmcp.server": _stub_module("fastmcp.server"),
        "fastmcp.server.dependencies": _stub_module(
            "fastmcp.server.dependencies", get_context=get_context
        ),
    }
    source = Path(__file__).resolve().parents[1] / "gco_mcp" / "tools" / "inference.py"
    module_name = "_coverage_100_isolated_inference_tool"
    spec = importlib.util.spec_from_file_location(module_name, source)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, modules):
        spec.loader.exec_module(module)
        yield module, run_cli, get_context


@pytest.mark.asyncio
async def test_mcp_context_warning_success_and_suppressed_failure() -> None:
    with _isolated_inference_tool() as (module, _run_cli, get_context):
        context = MagicMock()
        context.warning = AsyncMock()
        get_context.side_effect = None
        get_context.return_value = context

        await module._ctx_warning("destructive action")
        context.warning.assert_awaited_once_with("destructive action")

        context.warning.side_effect = RuntimeError("transport closed")
        await module._ctx_warning("still safe")


@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [
        ({}, ("inference", "list")),
        ({"region": "eu-west-1"}, ("inference", "list", "-r", "eu-west-1")),
    ],
)
def test_mcp_list_inference_filter_matrix(
    kwargs: dict[str, Any], expected: tuple[str, ...]
) -> None:
    with _isolated_inference_tool() as (module, run_cli, _get_context):
        assert module.list_inference_endpoints(**kwargs) == '{"ok":true}'
        run_cli.assert_called_once_with(*expected)


def test_mcp_configure_store_false_flags_are_explicit() -> None:
    with _isolated_inference_tool() as (module, run_cli, _get_context):
        assert (
            module.configure_mooncake_store("ep", cold_tier=False, enabled=False) == '{"ok":true}'
        )
        run_cli.assert_called_once_with(
            "inference", "configure-store", "ep", "--no-cold-tier", "--disable-store"
        )


# ---------------------------------------------------------------------------
# Follow-up cases for exact baseline branch outcomes
# ---------------------------------------------------------------------------


def test_target_region_update_reports_conditional_conflict(
    endpoint_store: tuple[InferenceEndpointStore, MagicMock],
) -> None:
    store, table = endpoint_store
    store.get_endpoint = MagicMock(  # type: ignore[method-assign]
        return_value={
            "lifecycle_id": "life-1",
            "updated_at": "snapshot",
            "desired_state": "running",
            "target_regions": ["us-east-1"],
            "cleanup_regions": ["us-east-1"],
            "region_generations": {"us-east-1": "east"},
        }
    )
    table.update_item.side_effect = _client_error("ConditionalCheckFailedException")

    assert (
        store.update_target_regions(
            "ep",
            ["us-east-1", "eu-west-1"],
            ["us-east-1", "eu-west-1"],
            {"eu-west-1": "west"},
            expected_lifecycle_id="life-1",
            expected_updated_at="snapshot",
        )
        is None
    )


def test_role_autoscaling_validator_accepts_absent_optional_bounds() -> None:
    from cli.inference import _validate_role_autoscaling_bounds

    assert _validate_role_autoscaling_bounds("prefill", {}) is None


@pytest.mark.parametrize(
    "kwargs",
    [
        {"framework": "unknown"},
        {"framework": "tgi", "mooncake_mode": "store"},
    ],
)
def test_manager_deploy_rejects_incompatible_framework_contracts(kwargs: dict[str, Any]) -> None:
    manager = _manager_with_store(MagicMock())
    with pytest.raises(ValueError, match="framework|Mooncake"):
        manager.deploy(
            "ep",
            image="image:v1",
            target_regions=["us-east-1"],
            **kwargs,
        )


def test_add_region_preserves_historical_cleanup_membership_without_reappending() -> None:
    endpoint = _mutable_endpoint()
    endpoint["cleanup_regions"] = ["us-east-1", "eu-west-1"]
    endpoint["region_generations"] = {"us-east-1": "east", "eu-west-1": "old-west"}
    store = MagicMock()
    store.get_endpoint.return_value = endpoint
    store.update_target_regions.return_value = {"endpoint_name": "ep"}
    manager = _manager_with_store(store)

    assert manager.add_region("ep", "eu-west-1") == {"endpoint_name": "ep"}
    cleanup = store.update_target_regions.call_args.args[2]
    assert cleanup == ["us-east-1", "eu-west-1"]


def test_remove_region_appends_target_missing_from_nonempty_cleanup_history() -> None:
    endpoint = _mutable_endpoint()
    endpoint["cleanup_regions"] = ["eu-west-1"]
    endpoint["region_generations"] = {"eu-west-1": "west"}
    store = MagicMock()
    store.get_endpoint.return_value = endpoint
    store.update_target_regions.return_value = {"endpoint_name": "ep"}
    manager = _manager_with_store(store)

    assert manager.remove_region("ep", "us-east-1") == {"endpoint_name": "ep"}
    cleanup = store.update_target_regions.call_args.args[2]
    assert cleanup == ["eu-west-1", "us-east-1"]


def test_cli_deploy_json_output_prints_result() -> None:
    manager = MagicMock()
    manager.deploy.return_value = {
        "endpoint_name": "ep",
        "target_regions": ["us-east-1"],
        "ingress_path": "/inference/ep",
    }

    result = _invoke_cli(
        ["-o", "json", "inference", "deploy", "ep", "-i", "image:v1"],
        manager=manager,
    )

    assert result.exit_code == 0, result.output
    assert '"endpoint_name": "ep"' in result.output


def test_cli_deploy_invalid_env_value_does_not_block_following_entries() -> None:
    manager = MagicMock()
    manager.deploy.return_value = {
        "endpoint_name": "ep",
        "target_regions": ["us-east-1"],
        "ingress_path": "/inference/ep",
    }

    result = _invoke_cli(
        [
            "inference",
            "deploy",
            "ep",
            "-i",
            "image:v1",
            "-e",
            "invalid",
            "-e",
            "VALID=value",
        ],
        manager=manager,
    )

    assert result.exit_code == 0, result.output
    assert manager.deploy.call_args.kwargs["env"] == {"VALID": "value"}


@pytest.mark.parametrize(
    ("transfer_args", "expected"),
    [
        (["--mooncake-protocol", "tcp"], {"protocol": "tcp"}),
        (["--mooncake-device-name", "efa_0"], {"device_name": "efa_0"}),
    ],
)
def test_cli_deploy_mooncake_transfer_partial_overrides(
    transfer_args: list[str], expected: dict[str, str]
) -> None:
    manager = MagicMock()
    manager.deploy.return_value = {
        "endpoint_name": "ep",
        "target_regions": ["us-east-1"],
        "ingress_path": "/inference/ep",
    }

    result = _invoke_cli(
        [
            "inference",
            "deploy",
            "ep",
            "-i",
            "image:v1",
            "--mooncake-mode",
            "disaggregated",
            "--mooncake-admin-key-secret",
            "admin",
            *transfer_args,
        ],
        manager=manager,
    )

    assert result.exit_code == 0, result.output
    assert manager.deploy.call_args.kwargs["mooncake_transfer"] == expected


def test_cli_deploy_proxy_image_without_named_secret_reports_auto_provisioning() -> None:
    manager = MagicMock()
    manager.deploy.return_value = {
        "endpoint_name": "ep",
        "target_regions": ["us-east-1"],
        "ingress_path": "/inference/ep",
    }

    result = _invoke_cli(
        [
            "inference",
            "deploy",
            "ep",
            "-i",
            "image:v1",
            "--mooncake-mode",
            "disaggregated",
            "--mooncake-proxy-image",
            "proxy:v1",
        ],
        manager=manager,
    )

    assert result.exit_code == 0, result.output
    assert "auto-provision" in result.output
    assert manager.deploy.call_args.kwargs["mooncake_proxy"] == {"image": "proxy:v1"}


def test_cli_status_skips_malformed_region_entry_and_continues_iteration() -> None:
    manager = MagicMock()
    manager.get_endpoint.return_value = {
        "endpoint_name": "ep",
        "desired_state": "running",
        "spec": {"image": "image:v1"},
        "region_status": {
            "malformed": "not-a-status-map",
            "us-east-1": {"state": "running", "last_sync": "now"},
        },
    }

    result = _invoke_cli(["inference", "status", "ep"], manager=manager)

    assert result.exit_code == 0, result.output
    assert "us-east-1" in result.output
    assert "not-a-status-map" not in result.output


def test_cli_delete_confirmation_path_can_proceed() -> None:
    manager = MagicMock()
    manager.delete.return_value = {"desired_state": "deleted"}

    result = _invoke_cli(["inference", "delete", "ep"], manager=manager, input_text="y\n")

    assert result.exit_code == 0, result.output
    manager.delete.assert_called_once()


def test_cli_canary_reports_validation_failure() -> None:
    manager = MagicMock()
    manager.canary_deploy.side_effect = ValueError("weight is invalid")

    result = _invoke_cli(["inference", "canary", "ep", "-i", "canary:v2"], manager=manager)

    assert result.exit_code != 0
    assert "weight is invalid" in result.output


def test_cli_streaming_uses_utf8_when_content_type_is_not_text() -> None:
    manager = MagicMock()
    manager.get_endpoint.return_value = {
        "ingress_path": "/inference/ep",
        "spec": {"image": "private/runtime:v1"},
    }
    aws_client = MagicMock()
    response = MagicMock(ok=True, status_code=200)
    response.headers = {"content-type": 123}
    response.iter_content.return_value = [b"token"]
    aws_client.make_authenticated_request.return_value = response

    result = _invoke_cli(
        ["inference", "invoke", "ep", "-p", "hello", "--path", "/v1", "--stream"],
        manager=manager,
        aws_client=aws_client,
    )

    assert result.exit_code == 0, result.output
    assert "token" in result.output
    response.close.assert_called_once_with()


def test_cli_buffered_empty_choices_returns_full_payload() -> None:
    manager = MagicMock()
    manager.get_endpoint.return_value = {
        "ingress_path": "/inference/ep",
        "spec": {"image": "private/runtime:v1"},
    }
    aws_client = MagicMock()
    response = MagicMock(ok=True)
    response.json.return_value = {"choices": []}
    aws_client.make_authenticated_request.return_value = response

    result = _invoke_cli(
        ["inference", "invoke", "ep", "-p", "hello", "--path", "/v1"],
        manager=manager,
        aws_client=aws_client,
    )

    assert result.exit_code == 0, result.output
    assert '"choices": []' in result.output


@pytest.mark.parametrize("command", ["promote", "rollback"])
def test_cli_canary_mutation_yes_path_calls_manager(command: str) -> None:
    manager = MagicMock()
    manager.get_endpoint.return_value = {
        "spec": {"image": "primary:v1", "canary": {"image": "canary:v2", "weight": 20}}
    }
    getattr(manager, f"{command}_canary").return_value = {"spec": {"image": "result:v2"}}

    result = _invoke_cli(["inference", command, "ep", "-y"], manager=manager)

    assert result.exit_code == 0, result.output
    getattr(manager, f"{command}_canary").assert_called_once_with("ep")


def test_cli_models_rejects_unsupported_persisted_framework() -> None:
    manager = MagicMock()
    manager.get_endpoint.return_value = {
        "ingress_path": "/inference/ep",
        "spec": {"framework": "custom", "image": "private/runtime:v1"},
    }

    result = _invoke_cli(["inference", "models", "ep"], manager=manager)

    assert result.exit_code != 0
    assert "unsupported persisted inference framework" in result.output


def test_cli_configure_store_json_success_prints_updated_record() -> None:
    manager = MagicMock()
    manager.get_endpoint.return_value = {
        "spec": {"mooncake": {"mode": "store", "store": {"enabled": True}}}
    }
    manager.configure_store.return_value = {"endpoint_name": "ep"}

    result = _invoke_cli(
        ["-o", "json", "inference", "configure-store", "ep", "--offload", "cpu"],
        manager=manager,
    )

    assert result.exit_code == 0, result.output
    assert '"endpoint_name": "ep"' in result.output


# ---------------------------------------------------------------------------
# Final baseline-union outcomes for TLS, CLI, and MCP adapters
# ---------------------------------------------------------------------------


def test_tls_valid_duration_and_paths_produce_runtime_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(TLS_CERT_FILE_ENV, "/tls/cert.pem")
    monkeypatch.setenv(TLS_KEY_FILE_ENV, "/tls/key.pem")
    monkeypatch.setenv("TLS_PROXY_POLL_SECONDS", "0.25")
    monkeypatch.setenv("GRACEFUL_SHUTDOWN_TIMEOUT_SECONDS", "0")

    config = load_proxy_config()

    assert _non_negative_number("TLS_PROXY_POLL_SECONDS", 5.0) == 0.25
    assert config.cert_file == Path("/tls/cert.pem")
    assert config.key_file == Path("/tls/key.pem")
    assert config.poll_seconds == 0.25
    assert config.graceful_shutdown_seconds == 0


def test_cli_invoke_tolerates_nonmapping_persisted_spec() -> None:
    manager = MagicMock()
    manager.get_endpoint.return_value = {
        "ingress_path": "/inference/ep",
        "spec": ["malformed"],
    }
    aws_client = MagicMock()
    response = MagicMock(ok=True)
    response.json.return_value = {"choices": [{"text": "ok"}]}
    aws_client.make_authenticated_request.return_value = response

    result = _invoke_cli(
        ["inference", "invoke", "ep", "-p", "hello", "--path", "/v1/completions"],
        manager=manager,
        aws_client=aws_client,
    )

    assert result.exit_code == 0, result.output
    assert aws_client.make_authenticated_request.call_args.kwargs["body"]["model"] == "ep"


def test_cli_streaming_honors_valid_declared_charset() -> None:
    manager = MagicMock()
    manager.get_endpoint.return_value = {
        "ingress_path": "/inference/ep",
        "spec": {"image": "private/runtime:v1"},
    }
    aws_client = MagicMock()
    response = MagicMock(ok=True, status_code=200)
    response.headers = {"content-type": "text/event-stream; charset=iso-8859-1"}
    response.iter_content.return_value = [b"caf\xe9"]
    aws_client.make_authenticated_request.return_value = response

    result = _invoke_cli(
        ["inference", "invoke", "ep", "-p", "hello", "--stream"],
        manager=manager,
        aws_client=aws_client,
    )

    assert result.exit_code == 0, result.output
    assert "café" in result.output
    response.close.assert_called_once_with()


@pytest.mark.parametrize("command", ["promote", "rollback"])
def test_cli_canary_mutation_accepts_interactive_confirmation(command: str) -> None:
    manager = MagicMock()
    manager.get_endpoint.return_value = {
        "spec": {
            "image": "primary:v1",
            "canary": {"image": "canary:v2", "weight": 20},
        }
    }
    getattr(manager, f"{command}_canary").return_value = {"spec": {"image": "result:v2"}}

    result = _invoke_cli(["inference", command, "ep"], manager=manager, input_text="y\n")

    assert result.exit_code == 0, result.output
    getattr(manager, f"{command}_canary").assert_called_once_with("ep")


def test_mcp_configure_store_omits_unspecified_boolean_flags() -> None:
    with _isolated_inference_tool() as (module, run_cli, _get_context):
        assert module.configure_mooncake_store("ep", offload="cpu") == '{"ok":true}'
        run_cli.assert_called_once_with("inference", "configure-store", "ep", "--offload", "cpu")


def test_cli_text_stream_without_declared_charset_defaults_to_utf8() -> None:
    manager = MagicMock()
    manager.get_endpoint.return_value = {
        "ingress_path": "/inference/ep",
        "spec": {"image": "private/runtime:v1"},
    }
    aws_client = MagicMock()
    response = MagicMock(ok=True, status_code=200)
    response.headers = {"content-type": "text/event-stream"}
    response.iter_content.return_value = ["token"]
    aws_client.make_authenticated_request.return_value = response

    result = _invoke_cli(
        ["inference", "invoke", "ep", "-p", "hello", "--stream"],
        manager=manager,
        aws_client=aws_client,
    )

    assert result.exit_code == 0, result.output
    assert "token" in result.output
    response.close.assert_called_once_with()
