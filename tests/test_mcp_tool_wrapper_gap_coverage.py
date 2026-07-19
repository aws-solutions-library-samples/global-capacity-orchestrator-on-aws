"""Behavior coverage for deterministic MCP CLI-wrapper branches.

The tool modules register functions at import time.  These tests load each source
file under a unique module name while replacing the MCP server, decorators,
feature flags, CLI runner, context dependencies, and backend helpers with local
stubs.  That keeps gated imports deterministic and cannot add tools to the shared
``run_mcp.mcp`` registry used by the rest of the suite.
"""

from __future__ import annotations

import asyncio
import importlib.util
import itertools
import json
import sys
import types
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, Mock, call, patch

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
TOOLS_ROOT = PROJECT_ROOT / "gco_mcp" / "tools"
CLI_RESULT = '{"wrapped":"cli"}'
LONG_TASK_RESULT = '{"wrapped":"long-task"}'

FLAG_CAPACITY_PURCHASE = "GCO_ENABLE_CAPACITY_PURCHASE"
FLAG_DESTRUCTIVE_OPERATIONS = "GCO_ENABLE_DESTRUCTIVE_OPERATIONS"
FLAG_IMAGE_PUBLISH = "GCO_ENABLE_IMAGE_PUBLISH"
FLAG_INFRASTRUCTURE_DEPLOY = "GCO_ENABLE_INFRASTRUCTURE_DEPLOY"
FLAG_INFRASTRUCTURE_DESTROY = "GCO_ENABLE_INFRASTRUCTURE_DESTROY"
FLAG_MODEL_UPLOAD = "GCO_ENABLE_MODEL_UPLOAD"

_MODULE_IDS = itertools.count()


class _McpStub:
    """Identity decorator provider that records no global registrations."""

    def tool(self, **_kwargs: Any) -> Any:
        return lambda function: function


class _InjectedDependency:
    """Constructible stand-in for FastMCP CurrentContext and Progress."""


class _TaskConfig:
    def __init__(self, mode: str) -> None:
        self.mode = mode


def _stub_module(name: str, **attributes: Any) -> types.ModuleType:
    module = types.ModuleType(name)
    for attribute, value in attributes.items():
        setattr(module, attribute, value)
    return module


@dataclass
class _LoadedTool:
    module: types.ModuleType
    run_cli: Mock
    long_task: AsyncMock
    dependencies: types.ModuleType
    get_task: Mock
    list_tasks: Mock
    tail_log: Mock
    resolve_local_path: Mock
    stage_upload_path: Mock
    load_cdk_json: Mock


@contextmanager
def _isolated_tool_module(
    tool_name: str,
    *enabled_flags: str,
) -> Iterator[_LoadedTool]:
    """Execute one tool source file against side-effect-free dependency stubs."""
    enabled = frozenset(enabled_flags)
    run_cli = Mock(return_value=CLI_RESULT)
    long_task = AsyncMock(return_value=LONG_TASK_RESULT)
    get_task = Mock()
    list_tasks = Mock(return_value=[])
    tail_log = Mock(return_value=[])
    resolve_local_path = Mock()
    stage_upload_path = Mock()
    load_cdk_json = Mock(return_value={"regional": []})

    cli_runner = _stub_module("cli_runner", _run_cli=run_cli)
    audit = _stub_module("audit", audit_logged=lambda function: function)
    server = _stub_module("server", mcp=_McpStub())
    feature_flags = _stub_module(
        "feature_flags",
        FLAG_CAPACITY_PURCHASE=FLAG_CAPACITY_PURCHASE,
        FLAG_DESTRUCTIVE_OPERATIONS=FLAG_DESTRUCTIVE_OPERATIONS,
        FLAG_IMAGE_PUBLISH=FLAG_IMAGE_PUBLISH,
        FLAG_INFRASTRUCTURE_DEPLOY=FLAG_INFRASTRUCTURE_DEPLOY,
        FLAG_INFRASTRUCTURE_DESTROY=FLAG_INFRASTRUCTURE_DESTROY,
        FLAG_MODEL_UPLOAD=FLAG_MODEL_UPLOAD,
        is_enabled=lambda flag: flag in enabled,
    )

    tools_package = _stub_module("tools")
    tools_package.__path__ = []  # type: ignore[attr-defined]
    long_task_module = _stub_module("tools._long_task", _run_long_task=long_task)
    task_status_module = _stub_module(
        "tools._task_status",
        get_task=get_task,
        list_tasks=list_tasks,
        tail_log=tail_log,
    )
    local_data = _stub_module(
        "local_data",
        resolve_local_path=resolve_local_path,
        stage_upload_path=stage_upload_path,
    )

    dependencies = _stub_module(
        "fastmcp.server.dependencies",
        CurrentContext=_InjectedDependency,
        Progress=_InjectedDependency,
        get_context=Mock(side_effect=LookupError("no active MCP context")),
    )
    fastmcp_package = _stub_module("fastmcp")
    fastmcp_package.__path__ = []  # type: ignore[attr-defined]
    fastmcp_server = _stub_module("fastmcp.server")
    fastmcp_server.__path__ = []  # type: ignore[attr-defined]
    fastmcp_tasks = _stub_module("fastmcp.server.tasks")
    fastmcp_tasks.__path__ = []  # type: ignore[attr-defined]
    task_config = _stub_module("fastmcp.server.tasks.config", TaskConfig=_TaskConfig)

    cli_package = _stub_module("cli")
    cli_package.__path__ = []  # type: ignore[attr-defined]
    cli_config = _stub_module("cli.config", _load_cdk_json=load_cdk_json)

    stubs = {
        "audit": audit,
        "cli": cli_package,
        "cli.config": cli_config,
        "cli_runner": cli_runner,
        "fastmcp": fastmcp_package,
        "fastmcp.server": fastmcp_server,
        "fastmcp.server.dependencies": dependencies,
        "fastmcp.server.tasks": fastmcp_tasks,
        "fastmcp.server.tasks.config": task_config,
        "feature_flags": feature_flags,
        "local_data": local_data,
        "server": server,
        "tools": tools_package,
        "tools._long_task": long_task_module,
        "tools._task_status": task_status_module,
    }
    unique_name = f"_mcp_wrapper_gap_{tool_name}_{next(_MODULE_IDS)}"
    source_path = TOOLS_ROOT / f"{tool_name}.py"
    spec = importlib.util.spec_from_file_location(unique_name, source_path)
    assert spec is not None and spec.loader is not None

    with patch.dict(sys.modules, stubs, clear=False):
        module = importlib.util.module_from_spec(spec)
        sys.modules[unique_name] = module
        spec.loader.exec_module(module)
        yield _LoadedTool(
            module=module,
            run_cli=run_cli,
            long_task=long_task,
            dependencies=dependencies,
            get_task=get_task,
            list_tasks=list_tasks,
            tail_log=tail_log,
            resolve_local_path=resolve_local_path,
            stage_upload_path=stage_upload_path,
            load_cdk_json=load_cdk_json,
        )


def _invoke(function: Any, *args: Any, **kwargs: Any) -> Any:
    result = function(*args, **kwargs)
    if asyncio.iscoroutine(result):
        return asyncio.run(result)
    return result


def _assert_cli_call(
    loaded: _LoadedTool,
    function_name: str,
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
    expected_argv: tuple[str, ...],
) -> None:
    loaded.run_cli.reset_mock()
    result = _invoke(getattr(loaded.module, function_name), *args, **kwargs)
    assert result == CLI_RESULT
    loaded.run_cli.assert_called_once_with(*expected_argv)


# ---------------------------------------------------------------------------
# capacity.py — option matrices, history/predict/find, and both gates
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("function_name", "args", "kwargs", "expected_argv"),
    [
        (
            "check_capacity",
            ("p5.48xlarge", "us-east-1"),
            {},
            ("capacity", "check", "-i", "p5.48xlarge", "-r", "us-east-1"),
        ),
        (
            "instance_info",
            ("g6.12xlarge",),
            {},
            ("capacity", "instance-info", "g6.12xlarge"),
        ),
        (
            "recommend_capacity",
            ("p4d.24xlarge", "us-west-2"),
            {"fault_tolerance": "high"},
            (
                "capacity",
                "recommend",
                "-i",
                "p4d.24xlarge",
                "-r",
                "us-west-2",
                "-f",
                "high",
            ),
        ),
        (
            "spot_prices",
            ("g5.2xlarge", "eu-west-1"),
            {},
            ("capacity", "spot-prices", "-i", "g5.2xlarge", "-r", "eu-west-1"),
        ),
        ("capacity_status", (), {}, ("capacity", "status")),
        (
            "capacity_status",
            (),
            {"region": "ap-southeast-2"},
            ("capacity", "status", "-r", "ap-southeast-2"),
        ),
        ("recommend_region", (), {}, ("capacity", "recommend-region")),
        (
            "recommend_region",
            (),
            {"gpu": True, "instance_type": "p6-b200", "gpu_count": 16},
            (
                "capacity",
                "recommend-region",
                "--gpu",
                "-i",
                "p6-b200",
                "--gpu-count",
                "16",
            ),
        ),
        ("list_reservations", (), {}, ("capacity", "reservations")),
        (
            "list_reservations",
            (),
            {"instance_type": "p5.48xlarge", "region": "us-east-2"},
            ("capacity", "reservations", "-i", "p5.48xlarge", "-r", "us-east-2"),
        ),
    ],
)
def test_capacity_fixed_and_small_option_wrappers(
    function_name: str,
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
    expected_argv: tuple[str, ...],
) -> None:
    with _isolated_tool_module("capacity") as loaded:
        _assert_cli_call(loaded, function_name, args, kwargs, expected_argv)


@pytest.mark.parametrize(
    ("kwargs", "expected_argv"),
    [
        ({"workload": "batch"}, ("capacity", "ai-recommend", "-w", "batch")),
        (
            {
                "workload": "train",
                "instance_type": "p5.48xlarge",
                "region": "us-west-2",
                "gpu": True,
                "min_gpus": 8,
                "min_memory_gb": 640,
                "fault_tolerance": "medium",
                "max_cost": 0.0,
                "model": "bedrock.model-v2",
            },
            (
                "capacity",
                "ai-recommend",
                "-w",
                "train",
                "-i",
                "p5.48xlarge",
                "-r",
                "us-west-2",
                "--gpu",
                "--min-gpus",
                "8",
                "--min-memory-gb",
                "640",
                "--fault-tolerance",
                "medium",
                "--max-cost",
                "0.0",
                "--model",
                "bedrock.model-v2",
            ),
        ),
    ],
)
def test_capacity_ai_recommend_option_matrix(
    kwargs: dict[str, Any], expected_argv: tuple[str, ...]
) -> None:
    with _isolated_tool_module("capacity") as loaded:
        _assert_cli_call(loaded, "ai_recommend", (), kwargs, expected_argv)


@pytest.mark.parametrize(
    ("kwargs", "expected_argv"),
    [
        (
            {"instance_type": "p5.48xlarge"},
            ("capacity", "reservation-check", "-i", "p5.48xlarge", "-c", "1"),
        ),
        (
            {
                "instance_type": "p6-b200",
                "regions": ["us-east-1", "us-west-2"],
                "count": 4,
                "include_blocks": False,
                "block_duration": 48,
                "block_duration_days": 0,
                "earliest_start": "2026-08-01",
                "latest_start": "2026-08-31",
            },
            (
                "capacity",
                "reservation-check",
                "-i",
                "p6-b200",
                "-c",
                "4",
                "-r",
                "us-east-1",
                "-r",
                "us-west-2",
                "--no-blocks",
                "--block-duration",
                "48",
                "--block-duration-days",
                "0",
                "--earliest-start",
                "2026-08-01",
                "--latest-start",
                "2026-08-31",
            ),
        ),
    ],
)
def test_capacity_reservation_check_matrix(
    kwargs: dict[str, Any], expected_argv: tuple[str, ...]
) -> None:
    with _isolated_tool_module("capacity") as loaded:
        _assert_cli_call(loaded, "reservation_check", (), kwargs, expected_argv)


def test_capacity_find_blocks_emits_zero_values_and_every_optional_flag() -> None:
    expected = (
        "capacity",
        "find-blocks",
        "-i",
        "p6-b300",
        "-r",
        "us-east-1",
        "-r",
        "eu-west-1",
        "-c",
        "2",
        "--duration-days",
        "0",
        "--duration-hours",
        "0",
        "--min-duration-days",
        "0",
        "--max-duration-days",
        "0",
        "--min-duration-hours",
        "0",
        "--max-duration-hours",
        "0",
        "--earliest-start",
        "2026-09-01",
        "--latest-start",
        "2026-09-30",
        "--find-longest",
    )
    kwargs = {
        "instance_type": "p6-b300",
        "regions": ["us-east-1", "eu-west-1"],
        "count": 2,
        "duration_days": 0,
        "duration_hours": 0,
        "min_duration_days": 0,
        "max_duration_days": 0,
        "min_duration_hours": 0,
        "max_duration_hours": 0,
        "earliest_start": "2026-09-01",
        "latest_start": "2026-09-30",
        "find_longest": True,
    }
    with _isolated_tool_module("capacity") as loaded:
        _assert_cli_call(loaded, "find_capacity_blocks", (), kwargs, expected)


def test_capacity_find_blocks_minimal_omits_every_optional_flag() -> None:
    with _isolated_tool_module("capacity") as loaded:
        _assert_cli_call(
            loaded,
            "find_capacity_blocks",
            ("p5.48xlarge",),
            {},
            ("capacity", "find-blocks", "-i", "p5.48xlarge"),
        )


@pytest.mark.parametrize(
    ("function_name", "subcommand"),
    [
        ("capacity_history_show", "show"),
        ("capacity_history_stats", "stats"),
        ("capacity_history_patterns", "patterns"),
    ],
)
def test_capacity_history_wrappers(function_name: str, subcommand: str) -> None:
    with _isolated_tool_module("capacity") as loaded:
        _assert_cli_call(
            loaded,
            function_name,
            ("g6.48xlarge", "us-east-2"),
            {"hours": 336},
            (
                "capacity",
                "history",
                subcommand,
                "-i",
                "g6.48xlarge",
                "-r",
                "us-east-2",
                "-H",
                "336",
            ),
        )


@pytest.mark.parametrize(
    ("kwargs", "expected_argv"),
    [
        (
            {"instance_type": "p5.48xlarge"},
            ("capacity", "predict", "-i", "p5.48xlarge", "-H", "168"),
        ),
        (
            {"instance_type": "p5.48xlarge", "region": "us-east-1", "hours": 24},
            (
                "capacity",
                "predict",
                "-i",
                "p5.48xlarge",
                "-H",
                "24",
                "-r",
                "us-east-1",
            ),
        ),
        (
            {
                "instance_type": "p5.48xlarge",
                "region": "ignored-region",
                "all_regions": True,
            },
            (
                "capacity",
                "predict",
                "-i",
                "p5.48xlarge",
                "-H",
                "168",
                "--all-regions",
            ),
        ),
    ],
)
def test_capacity_predict_scope_precedence(
    kwargs: dict[str, Any], expected_argv: tuple[str, ...]
) -> None:
    with _isolated_tool_module("capacity") as loaded:
        _assert_cli_call(loaded, "capacity_predict", (), kwargs, expected_argv)


@pytest.mark.parametrize(
    ("kwargs", "expected_argv"),
    [
        ({}, ("capacity", "find-reservations")),
        (
            {
                "instance_type": "p6-b200",
                "regions": ["us-east-1", "us-west-2"],
                "count": 8,
                "state": "all",
                "pricing": False,
            },
            (
                "capacity",
                "find-reservations",
                "-i",
                "p6-b200",
                "-r",
                "us-east-1",
                "-r",
                "us-west-2",
                "-c",
                "8",
                "--state",
                "all",
                "--no-pricing",
            ),
        ),
    ],
)
def test_capacity_find_reservations_matrix(
    kwargs: dict[str, Any], expected_argv: tuple[str, ...]
) -> None:
    with _isolated_tool_module("capacity") as loaded:
        _assert_cli_call(loaded, "find_capacity_reservations", (), kwargs, expected_argv)


def test_capacity_gated_definitions_are_isolated_and_flag_specific() -> None:
    with _isolated_tool_module("capacity") as disabled:
        assert not hasattr(disabled.module, "reserve_capacity")
        assert not hasattr(disabled.module, "create_reservation")
        assert not hasattr(disabled.module, "cancel_reservation")
    with _isolated_tool_module("capacity", FLAG_CAPACITY_PURCHASE) as purchase:
        assert hasattr(purchase.module, "reserve_capacity")
        assert hasattr(purchase.module, "create_reservation")
        assert not hasattr(purchase.module, "cancel_reservation")
    with _isolated_tool_module("capacity", FLAG_DESTRUCTIVE_OPERATIONS) as destructive:
        assert not hasattr(destructive.module, "reserve_capacity")
        assert hasattr(destructive.module, "cancel_reservation")


@pytest.mark.parametrize(
    ("function_name", "args", "kwargs", "expected_argv"),
    [
        (
            "reserve_capacity",
            ("cb-offering", "us-east-1"),
            {},
            ("capacity", "reserve", "-o", "cb-offering", "-r", "us-east-1"),
        ),
        (
            "reserve_capacity",
            ("cb-offering", "us-east-1"),
            {"dry_run": True},
            (
                "capacity",
                "reserve",
                "-o",
                "cb-offering",
                "-r",
                "us-east-1",
                "--dry-run",
            ),
        ),
        (
            "create_reservation",
            ("p5.48xlarge", "us-east-1", "us-east-1a"),
            {},
            (
                "capacity",
                "create-reservation",
                "-i",
                "p5.48xlarge",
                "-r",
                "us-east-1",
                "-z",
                "us-east-1a",
                "-c",
                "1",
            ),
        ),
        (
            "create_reservation",
            ("p6-b200", "us-west-2", "us-west-2b"),
            {
                "count": 4,
                "platform": "Linux with SQL Server",
                "tenancy": "dedicated",
                "match_criteria": "targeted",
                "end_date": "2026-12-31",
                "ebs_optimized": True,
                "dry_run": True,
            },
            (
                "capacity",
                "create-reservation",
                "-i",
                "p6-b200",
                "-r",
                "us-west-2",
                "-z",
                "us-west-2b",
                "-c",
                "4",
                "--platform",
                "Linux with SQL Server",
                "--tenancy",
                "dedicated",
                "--match-criteria",
                "targeted",
                "--end-date",
                "2026-12-31",
                "--ebs-optimized",
                "--dry-run",
            ),
        ),
        (
            "cancel_reservation",
            ("cr-123", "eu-central-1"),
            {},
            (
                "capacity",
                "cancel-reservation",
                "-o",
                "cr-123",
                "-r",
                "eu-central-1",
                "-y",
            ),
        ),
        (
            "cancel_reservation",
            ("cr-123", "eu-central-1"),
            {"dry_run": True},
            (
                "capacity",
                "cancel-reservation",
                "-o",
                "cr-123",
                "-r",
                "eu-central-1",
                "-y",
                "--dry-run",
            ),
        ),
    ],
)
def test_capacity_gated_wrapper_argv(
    function_name: str,
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
    expected_argv: tuple[str, ...],
) -> None:
    with _isolated_tool_module(
        "capacity", FLAG_CAPACITY_PURCHASE, FLAG_DESTRUCTIVE_OPERATIONS
    ) as loaded:
        _assert_cli_call(loaded, function_name, args, kwargs, expected_argv)


# ---------------------------------------------------------------------------
# monitoring.py — every wrapper plus all context-warning outcomes
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("function_name", "expected_argv"),
    [
        ("monitoring_status", ("monitoring", "status")),
        ("monitoring_users_list", ("monitoring", "users", "list")),
        ("enable_monitoring", ("monitoring", "enable", "-y")),
        ("disable_monitoring", ("monitoring", "disable", "-y")),
    ],
)
def test_monitoring_fixed_wrappers(function_name: str, expected_argv: tuple[str, ...]) -> None:
    with _isolated_tool_module("monitoring") as loaded:
        _assert_cli_call(loaded, function_name, (), {}, expected_argv)


@pytest.mark.parametrize(
    ("email", "expected_argv"),
    [
        (
            None,
            (
                "monitoring",
                "users",
                "add",
                "--username",
                "alice",
                "--password",
                "secret",
            ),
        ),
        (
            "alice@example.com",
            (
                "monitoring",
                "users",
                "add",
                "--username",
                "alice",
                "--password",
                "secret",
                "--email",
                "alice@example.com",
            ),
        ),
    ],
)
def test_monitoring_user_add_email_branch(
    email: str | None, expected_argv: tuple[str, ...]
) -> None:
    with _isolated_tool_module("monitoring") as loaded:
        _assert_cli_call(
            loaded,
            "monitoring_user_add",
            ("alice", "secret"),
            {"email": email},
            expected_argv,
        )


def test_monitoring_context_warning_succeeds() -> None:
    with _isolated_tool_module("monitoring") as loaded:
        context = MagicMock()
        context.warning = AsyncMock()
        loaded.dependencies.get_context.side_effect = None
        loaded.dependencies.get_context.return_value = context
        assert asyncio.run(loaded.module._ctx_warning("careful")) is None
        context.warning.assert_awaited_once_with("careful")


def test_monitoring_context_warning_swallows_dispatch_failure() -> None:
    with _isolated_tool_module("monitoring") as loaded:
        context = MagicMock()
        context.warning = AsyncMock(side_effect=RuntimeError("transport closed"))
        loaded.dependencies.get_context.side_effect = None
        loaded.dependencies.get_context.return_value = context
        assert asyncio.run(loaded.module._ctx_warning("careful")) is None
        context.warning.assert_awaited_once_with("careful")


def test_monitoring_context_warning_without_context_is_noop() -> None:
    with _isolated_tool_module("monitoring") as loaded:
        assert asyncio.run(loaded.module._ctx_warning("careful")) is None
        loaded.dependencies.get_context.assert_called_once_with()


def test_monitoring_gated_remove_warns_and_uses_exact_argv() -> None:
    with _isolated_tool_module("monitoring", FLAG_DESTRUCTIVE_OPERATIONS) as loaded:
        warning = AsyncMock()
        loaded.module._ctx_warning = warning
        _assert_cli_call(
            loaded,
            "monitoring_user_remove",
            ("alice",),
            {},
            (
                "monitoring",
                "users",
                "remove",
                "--username",
                "alice",
                "--yes",
            ),
        )
        warning.assert_awaited_once_with("Removing Grafana user 'alice' — this cannot be undone.")


# ---------------------------------------------------------------------------
# images.py — publish argv and remaining mirror/destructive branches
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("function_name", "args", "kwargs", "expected_argv"),
    [
        (
            "images_build",
            ("./context", "trainer"),
            {},
            (
                "gco",
                "images",
                "build",
                "./context",
                "--name",
                "trainer",
                "--dockerfile",
                "Dockerfile",
                "--platform",
                "linux/amd64",
            ),
        ),
        (
            "images_build",
            ("./context", "trainer"),
            {
                "tag": "v2",
                "dockerfile": "docker/Dockerfile.gpu",
                "platform": "linux/arm64",
                "retain": True,
            },
            (
                "gco",
                "images",
                "build",
                "./context",
                "--name",
                "trainer",
                "--tag",
                "v2",
                "--dockerfile",
                "docker/Dockerfile.gpu",
                "--platform",
                "linux/arm64",
                "--retain",
            ),
        ),
        (
            "images_push",
            ("trainer", "v1", "local/trainer:v1"),
            {},
            (
                "gco",
                "images",
                "push",
                "trainer",
                "--tag",
                "v1",
                "--local-image",
                "local/trainer:v1",
            ),
        ),
        (
            "images_push",
            ("trainer", "v1", "local/trainer:v1"),
            {"retain": True},
            (
                "gco",
                "images",
                "push",
                "trainer",
                "--tag",
                "v1",
                "--local-image",
                "local/trainer:v1",
                "--retain",
            ),
        ),
    ],
)
def test_image_publish_long_task_argv(
    function_name: str,
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
    expected_argv: tuple[str, ...],
) -> None:
    with _isolated_tool_module("images", FLAG_IMAGE_PUBLISH) as loaded:
        context = object()
        progress = object()
        result = asyncio.run(
            getattr(loaded.module, function_name)(
                *args,
                **kwargs,
                ctx=context,
                progress=progress,
            )
        )
        assert result == LONG_TASK_RESULT
        loaded.long_task.assert_awaited_once_with(
            list(expected_argv),
            ctx=context,
            progress=progress,
            is_stack_op=False,
        )


def test_images_mirror_forwards_false_skip_and_captures_log() -> None:
    with _isolated_tool_module("images", FLAG_IMAGE_PUBLISH) as loaded:
        mirror = MagicMock()

        def mirror_images(*_args: Any, **kwargs: Any) -> dict[str, Any]:
            kwargs["log"]("copied one")
            return {"mirrored": ["image:v1"], "skipped": []}

        mirror.mirror_images.side_effect = mirror_images
        loaded.module._get_image_mirror = Mock(return_value=mirror)
        result = asyncio.run(
            loaded.module.images_mirror(
                "eu-west-1",
                ecr_namespace="gco/custom",
                skip_existing=False,
            )
        )
        assert result == json.dumps(
            {"mirrored": ["image:v1"], "skipped": [], "log": ["copied one"]}
        )
        mirror.mirror_images.assert_called_once_with(
            "eu-west-1",
            ecr_namespace="gco/custom",
            skip_existing=False,
            log=mirror.mirror_images.call_args.kwargs["log"],
        )


def test_images_destructive_cleanup_scope_and_prune_warning_branches() -> None:
    with _isolated_tool_module("images", FLAG_DESTRUCTIVE_OPERATIONS) as loaded:
        manager = MagicMock()
        manager.cleanup.side_effect = [
            {"scope": "repo"},
            {"scope": "all"},
        ]
        manager.prune.side_effect = [
            {"dry_run": True},
            {"dry_run": False},
        ]
        loaded.module._get_manager = Mock(return_value=manager)
        loaded.module._ctx_warning = AsyncMock()

        assert asyncio.run(loaded.module.images_cleanup(name="trainer")) == json.dumps(
            {"scope": "repo"}
        )
        assert asyncio.run(loaded.module.images_cleanup(all=True)) == json.dumps({"scope": "all"})
        assert asyncio.run(loaded.module.images_prune()) == json.dumps({"dry_run": True})
        assert asyncio.run(loaded.module.images_prune(dry_run=False)) == json.dumps(
            {"dry_run": False}
        )

        assert manager.cleanup.call_args_list == [
            call(name="trainer", all=False),
            call(name=None, all=True),
        ]
        assert manager.prune.call_args_list == [call(dry_run=True), call(dry_run=False)]
        assert loaded.module._ctx_warning.await_args_list == [
            call("Cleaning untagged images from gco/trainer — this cannot be undone."),
            call("Cleaning untagged images from all repos — this cannot be undone."),
            call("Pruning untagged images older than 30 days — this cannot be undone."),
        ]


# ---------------------------------------------------------------------------
# inference.py — parse guards, store flags, disaggregated deploy/delete gate
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("not-json", False),
        ("[]", False),
        ('{"spec":[]}', False),
        ('{"spec":{"mooncake":[]}}', False),
        ('{"spec":{"mooncake":{"mode":"store"}}}', False),
        ('{"spec":{"mooncake":{"mode":"disaggregated"}}}', True),
        ('{"spec":{"mooncake":{"mode":"both"}}}', True),
    ],
)
def test_inference_disaggregated_status_parse_guards(raw: str, expected: bool) -> None:
    with _isolated_tool_module("inference") as loaded:
        loaded.run_cli.return_value = raw
        assert loaded.module._endpoint_is_disaggregated("endpoint-a") is expected
        loaded.run_cli.assert_called_once_with("inference", "status", "endpoint-a")


def test_inference_configure_store_requires_a_setting() -> None:
    with _isolated_tool_module("inference") as loaded:
        with pytest.raises(ValueError, match="At least one Mooncake store setting"):
            loaded.module.configure_mooncake_store("endpoint-a")
        loaded.run_cli.assert_not_called()


@pytest.mark.parametrize(
    ("kwargs", "expected_argv"),
    [
        (
            {
                "cold_tier": False,
                "offload": "none",
                "global_segment_size": 0,
                "local_buffer_size": 0,
                "enabled": False,
            },
            (
                "inference",
                "configure-store",
                "endpoint-a",
                "--no-cold-tier",
                "--offload",
                "none",
                "--global-segment-size",
                "0",
                "--local-buffer-size",
                "0",
                "--disable-store",
            ),
        ),
        (
            {"cold_tier": True, "enabled": True},
            (
                "inference",
                "configure-store",
                "endpoint-a",
                "--cold-tier",
                "--enable-store",
            ),
        ),
    ],
)
def test_inference_configure_store_boolean_and_zero_matrix(
    kwargs: dict[str, Any], expected_argv: tuple[str, ...]
) -> None:
    with _isolated_tool_module("inference") as loaded:
        _assert_cli_call(
            loaded,
            "configure_mooncake_store",
            ("endpoint-a",),
            kwargs,
            expected_argv,
        )


@pytest.mark.parametrize(
    ("kwargs", "expected_argv"),
    [
        (
            {"name": "split"},
            (
                "inference",
                "deploy",
                "split",
                "--mooncake-mode",
                "disaggregated",
                "--prefill-replicas",
                "1",
                "--decode-replicas",
                "1",
                "--gpu-count",
                "1",
                "--port",
                "8000",
            ),
        ),
        (
            {
                "name": "split",
                "image": "repo/mooncake:v2",
                "prefill": 2,
                "decode": 5,
                "mooncake_mode": "both",
                "gpu_count": 8,
                "port": 9000,
                "region": "us-west-2",
                "env_vars": ["A=1", "B=2"],
            },
            (
                "inference",
                "deploy",
                "split",
                "--mooncake-mode",
                "both",
                "--prefill-replicas",
                "2",
                "--decode-replicas",
                "5",
                "--gpu-count",
                "8",
                "--port",
                "9000",
                "-i",
                "repo/mooncake:v2",
                "-r",
                "us-west-2",
                "-e",
                "A=1",
                "-e",
                "B=2",
            ),
        ),
    ],
)
def test_inference_disaggregated_deploy_matrix(
    kwargs: dict[str, Any], expected_argv: tuple[str, ...]
) -> None:
    with _isolated_tool_module("inference") as loaded:
        _assert_cli_call(
            loaded,
            "deploy_disaggregated_inference",
            (),
            kwargs,
            expected_argv,
        )


def test_inference_delete_refuses_disaggregated_when_flag_changes_off() -> None:
    with _isolated_tool_module("inference", FLAG_DESTRUCTIVE_OPERATIONS) as loaded:
        loaded.module.is_enabled = Mock(return_value=False)
        loaded.module._endpoint_is_disaggregated = Mock(return_value=True)
        loaded.module._ctx_warning = AsyncMock()
        result = asyncio.run(loaded.module.delete_inference("split"))
        expected_message = (
            "Refusing to delete disaggregated endpoint 'split': destructive operations "
            "are disabled. Set GCO_ENABLE_DESTRUCTIVE_OPERATIONS=true to allow this deletion."
        )
        assert result == json.dumps(
            {"error": expected_message, "destructive_operations_disabled": True}
        )
        loaded.module._ctx_warning.assert_awaited_once_with(expected_message)
        loaded.run_cli.assert_not_called()


@pytest.mark.parametrize(("flag_enabled", "disaggregated"), [(True, True), (False, False)])
def test_inference_delete_allowed_paths(flag_enabled: bool, disaggregated: bool) -> None:
    with _isolated_tool_module("inference", FLAG_DESTRUCTIVE_OPERATIONS) as loaded:
        loaded.module.is_enabled = Mock(return_value=flag_enabled)
        loaded.module._endpoint_is_disaggregated = Mock(return_value=disaggregated)
        loaded.module._ctx_warning = AsyncMock()
        _assert_cli_call(
            loaded,
            "delete_inference",
            ("endpoint-a",),
            {},
            ("inference", "delete", "endpoint-a", "-y"),
        )
        loaded.module._ctx_warning.assert_awaited_once_with(
            "Deleting inference endpoint 'endpoint-a' — this cannot be undone."
        )
        if flag_enabled:
            loaded.module._endpoint_is_disaggregated.assert_not_called()
        else:
            loaded.module._endpoint_is_disaggregated.assert_called_once_with("endpoint-a")


# ---------------------------------------------------------------------------
# stacks.py — option precedence and best-effort stack-count branches
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("config", "expected"),
    [
        ({"regional": ["us-east-1", "us-west-2"]}, 5),
        ({}, None),
        ({"regional": []}, 3),
        ([], None),
        ({"regional": "us-east-1"}, None),
    ],
)
def test_stacks_expected_count_shapes(config: Any, expected: int | None) -> None:
    with _isolated_tool_module("stacks") as loaded:
        loaded.load_cdk_json.return_value = config
        assert loaded.module._expected_stack_count_for_all() == expected


def test_stacks_expected_count_swallows_loader_failure() -> None:
    with _isolated_tool_module("stacks") as loaded:
        loaded.load_cdk_json.side_effect = RuntimeError("bad cdk.json")
        assert loaded.module._expected_stack_count_for_all() is None


@pytest.mark.parametrize(
    ("function_name", "args", "kwargs", "expected_argv"),
    [
        ("setup_cluster_access", (), {}, ("stacks", "access")),
        (
            "setup_cluster_access",
            (),
            {"cluster": "gco-prod", "region": "us-east-1"},
            ("stacks", "access", "-c", "gco-prod", "-r", "us-east-1"),
        ),
        ("stack_diff", (), {}, ("stacks", "diff")),
        ("stack_diff", ("gco-global",), {}, ("stacks", "diff", "gco-global")),
        ("stack_synth", (), {"quiet": False}, ("stacks", "synth")),
        (
            "stack_synth",
            ("gco-global",),
            {},
            ("stacks", "synth", "gco-global", "--quiet"),
        ),
        (
            "addons_status",
            (),
            {"region": "us-east-1"},
            ("stacks", "addons", "status", "-r", "us-east-1"),
        ),
        (
            "addons_status",
            (),
            {"region": "ignored", "all_regions": True},
            ("stacks", "addons", "status", "--all-regions"),
        ),
    ],
)
def test_stacks_wrapper_option_matrix(
    function_name: str,
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
    expected_argv: tuple[str, ...],
) -> None:
    with _isolated_tool_module("stacks") as loaded:
        _assert_cli_call(loaded, function_name, args, kwargs, expected_argv)


@pytest.mark.parametrize(
    ("kwargs", "expected_argv"),
    [
        ({}, ("stacks", "addons", "install")),
        (
            {"region": "eu-west-1"},
            ("stacks", "addons", "install", "-r", "eu-west-1"),
        ),
        (
            {"region": "ignored", "all_regions": True},
            ("stacks", "addons", "install", "--all-regions"),
        ),
    ],
)
def test_stacks_gated_addons_install_precedence(
    kwargs: dict[str, Any], expected_argv: tuple[str, ...]
) -> None:
    with _isolated_tool_module("stacks", FLAG_INFRASTRUCTURE_DEPLOY) as loaded:
        _assert_cli_call(loaded, "addons_install", (), kwargs, expected_argv)


# ---------------------------------------------------------------------------
# Remaining deterministic wrappers called out by the CI gap report
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("kwargs", "expected_argv"),
    [
        (
            {
                "name": "reserved",
                "region": "us-east-1",
                "capacity_reservation_id": "cr-123",
            },
            (
                "nodepools",
                "create-capacity-block",
                "-n",
                "reserved",
                "-r",
                "us-east-1",
                "-c",
                "cr-123",
                "--max-nodes",
                "100",
            ),
        ),
        (
            {
                "name": "reserved",
                "region": "us-east-1",
                "capacity_reservation_id": "cr-123",
                "instance_type": ["p5.48xlarge", "p6-b200.48xlarge"],
                "max_nodes": 12,
                "fallback_on_demand": True,
                "efa": True,
            },
            (
                "nodepools",
                "create-capacity-block",
                "-n",
                "reserved",
                "-r",
                "us-east-1",
                "-c",
                "cr-123",
                "--max-nodes",
                "12",
                "-i",
                "p5.48xlarge",
                "-i",
                "p6-b200.48xlarge",
                "--fallback-on-demand",
                "--efa",
            ),
        ),
    ],
)
def test_nodepool_capacity_block_matrix(
    kwargs: dict[str, Any], expected_argv: tuple[str, ...]
) -> None:
    with _isolated_tool_module("nodepools") as loaded:
        _assert_cli_call(
            loaded,
            "nodepools_create_capacity_block",
            (),
            kwargs,
            expected_argv,
        )


@pytest.mark.parametrize("failure_type", [OSError, ValueError])
def test_models_upload_rejects_resolver_failures(failure_type: type[Exception]) -> None:
    with _isolated_tool_module("models", FLAG_MODEL_UPLOAD) as loaded:
        loaded.resolve_local_path.side_effect = failure_type("outside local root")
        result = asyncio.run(loaded.module.models_upload("llama", "../weights"))
        assert result == json.dumps(
            {"error": "outside local root", "code": "local_data_path_rejected"}
        )
        loaded.run_cli.assert_not_called()


def test_models_upload_reports_staging_failure_without_cli_call() -> None:
    with _isolated_tool_module("models", FLAG_MODEL_UPLOAD) as loaded:
        contract = object()
        loaded.resolve_local_path.return_value = contract
        loaded.stage_upload_path.side_effect = OSError("snapshot failed")
        result = asyncio.run(loaded.module.models_upload("llama", "weights"))
        assert result == json.dumps(
            {"error": "snapshot failed", "code": "local_data_path_rejected"}
        )
        loaded.resolve_local_path.assert_called_once_with(
            "weights", require_exists=True, purpose="Model upload"
        )
        loaded.stage_upload_path.assert_called_once_with(contract)
        loaded.run_cli.assert_not_called()


@pytest.mark.parametrize(
    ("function_name", "args", "kwargs", "expected_argv"),
    [
        ("analytics_doctor", (), {}, ("analytics", "doctor")),
        ("analytics_status", (), {}, ("analytics", "status")),
        ("analytics_users_list", (), {}, ("analytics", "users", "list")),
        ("enable_analytics", (), {}, ("analytics", "enable", "-y")),
        ("disable_analytics", (), {}, ("analytics", "disable", "-y")),
        (
            "analytics_user_add",
            ("alice", "alice@example.com"),
            {},
            ("analytics", "users", "add", "alice", "--email", "alice@example.com"),
        ),
        (
            "analytics_login_url",
            ("alice",),
            {},
            ("analytics", "studio", "login", "--username", "alice"),
        ),
        (
            "analytics_login_url",
            ("alice",),
            {"password": "secret"},
            (
                "analytics",
                "studio",
                "login",
                "--username",
                "alice",
                "--password",
                "secret",
            ),
        ),
    ],
)
def test_analytics_wrapper_matrix(
    function_name: str,
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
    expected_argv: tuple[str, ...],
) -> None:
    with _isolated_tool_module("analytics") as loaded:
        _assert_cli_call(loaded, function_name, args, kwargs, expected_argv)


@pytest.mark.parametrize(
    ("function_name", "args", "kwargs", "expected_argv"),
    [
        ("list_jobs", (), {}, ("jobs", "list", "--all-regions")),
        (
            "list_jobs",
            (),
            {"region": "us-west-2", "namespace": "ml", "status": "running"},
            ("jobs", "list", "-r", "us-west-2", "-n", "ml", "-s", "running"),
        ),
        (
            "submit_job_sqs",
            ("job.yaml", "us-east-1"),
            {"priority": 0},
            ("jobs", "submit-sqs", "job.yaml", "-r", "us-east-1", "--priority", "0"),
        ),
        (
            "submit_job_api",
            ("job.yaml",),
            {"namespace": "training"},
            ("jobs", "submit", "job.yaml", "-n", "training"),
        ),
        (
            "get_pod_logs",
            ("job-a", "job-a-pod", "eu-west-1"),
            {"namespace": "training", "tail": 25, "container": "worker"},
            (
                "jobs",
                "pod-logs",
                "job-a",
                "job-a-pod",
                "-r",
                "eu-west-1",
                "-n",
                "training",
                "--tail",
                "25",
                "--container",
                "worker",
            ),
        ),
        ("cluster_health", (), {}, ("jobs", "health", "--all-regions")),
        (
            "cluster_health",
            ("us-east-2",),
            {},
            ("jobs", "health", "-r", "us-east-2"),
        ),
        ("queue_status", (), {}, ("jobs", "queue-status", "--all-regions")),
        (
            "queue_status",
            ("ap-northeast-1",),
            {},
            ("jobs", "queue-status", "-r", "ap-northeast-1"),
        ),
    ],
)
def test_jobs_wrapper_branch_matrix(
    function_name: str,
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
    expected_argv: tuple[str, ...],
) -> None:
    with _isolated_tool_module("jobs") as loaded:
        _assert_cli_call(loaded, function_name, args, kwargs, expected_argv)


@pytest.mark.parametrize(
    ("function_name", "args", "kwargs", "expected_argv"),
    [
        ("webhooks_list", (), {}, ("webhooks", "list")),
        (
            "webhooks_get",
            ("alerts",),
            {"region": "us-east-1"},
            ("webhooks", "get", "alerts", "-r", "us-east-1"),
        ),
        (
            "webhooks_create",
            ("https://example.test/hook", ["job.started", "job.failed"]),
            {
                "namespace": "training",
                "region": "us-west-2",
                "secret": "hmac-secret",
            },
            (
                "webhooks",
                "create",
                "--url",
                "https://example.test/hook",
                "--event",
                "job.started",
                "--event",
                "job.failed",
                "--namespace",
                "training",
                "-r",
                "us-west-2",
                "--secret",
                "hmac-secret",
            ),
        ),
    ],
)
def test_webhook_wrapper_matrix(
    function_name: str,
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
    expected_argv: tuple[str, ...],
) -> None:
    with _isolated_tool_module("webhooks") as loaded:
        _assert_cli_call(loaded, function_name, args, kwargs, expected_argv)


def test_webhook_create_rejects_empty_events_before_cli() -> None:
    with _isolated_tool_module("webhooks") as loaded:
        with pytest.raises(ValueError, match="at least one webhook event"):
            _invoke(loaded.module.webhooks_create, "https://example.test/hook", [])
        loaded.run_cli.assert_not_called()


def test_webhook_gated_delete_warns_and_orders_region_after_yes() -> None:
    with _isolated_tool_module("webhooks", FLAG_DESTRUCTIVE_OPERATIONS) as loaded:
        loaded.module._ctx_warning = AsyncMock()
        _assert_cli_call(
            loaded,
            "delete_webhook",
            ("alerts",),
            {"region": "us-east-1"},
            ("webhooks", "delete", "alerts", "-y", "-r", "us-east-1"),
        )
        loaded.module._ctx_warning.assert_awaited_once_with(
            "Deleting webhook 'alerts' — this cannot be undone."
        )


@pytest.mark.parametrize("limit", [0, -1])
def test_task_status_nonpositive_limit_is_unbounded(limit: int) -> None:
    records = [{"task_id": "new"}, {"task_id": "old"}]
    with _isolated_tool_module("tasks") as loaded:
        loaded.list_tasks.return_value = records
        result = loaded.module.task_status(limit=limit)
        assert result == json.dumps({"tasks": records}, indent=2, sort_keys=True)
        loaded.list_tasks.assert_called_once_with()


def test_task_status_positive_limit_slices_after_listing() -> None:
    records = [{"task_id": "new"}, {"task_id": "old"}]
    with _isolated_tool_module("tasks") as loaded:
        loaded.list_tasks.return_value = records
        result = loaded.module.task_status(limit=1)
        assert result == json.dumps({"tasks": records[:1]}, indent=2, sort_keys=True)


def test_task_prune_rejects_negative_keep_before_cli() -> None:
    with _isolated_tool_module("tasks", FLAG_DESTRUCTIVE_OPERATIONS) as loaded:
        with pytest.raises(ValueError, match="keep must be non-negative"):
            loaded.module.task_prune(-1)
        loaded.run_cli.assert_not_called()


def test_task_prune_accepts_zero_and_returns_cli_value() -> None:
    with _isolated_tool_module("tasks", FLAG_DESTRUCTIVE_OPERATIONS) as loaded:
        _assert_cli_call(
            loaded,
            "task_prune",
            (0,),
            {},
            ("tasks", "prune", "--keep", "0", "--yes"),
        )
