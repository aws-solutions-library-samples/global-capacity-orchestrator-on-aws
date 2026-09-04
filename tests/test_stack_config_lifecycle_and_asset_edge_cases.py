"""Behavior-focused tests for the stack-domain baseline coverage gaps.

The tests in this module isolate filesystem, subprocess, AWS, CDK, and MCP
boundaries.  They exercise observable contracts rather than executing real
infrastructure operations.
"""

from __future__ import annotations

import asyncio
import base64
import builtins
import errno
import importlib.util
import io
import itertools
import json
import os
import stat
import subprocess
import sys
import types
import zlib
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, Mock, call, patch

import pytest
from botocore.exceptions import ClientError
from click.testing import CliRunner

from cli.stacks import StackManager as _StackManager

PROJECT_ROOT = Path(__file__).resolve().parents[1]

# tests/conftest.py safely no-ops destructive cleanup methods for ordinary
# tests. Capture the production callables before those function-scoped patches
# activate; the focused tests below invoke them only with fully mocked clients.
_REAL_CLEANUP_BACKUP_VAULT = _StackManager._cleanup_backup_vault
_REAL_COLLECT_IMPLICIT_LOG_GROUPS = _StackManager._collect_implicit_log_groups
_REAL_CLEANUP_IMPLICIT_LOG_GROUPS = _StackManager._cleanup_implicit_log_groups
_REAL_CLEANUP_EKS_SECURITY_GROUPS = _StackManager._cleanup_eks_security_groups
_REAL_CLEANUP_CLUSTER_VOLUMES = _StackManager._cleanup_cluster_volumes


def _client_error(code: str, message: str = "failure", operation: str = "operation") -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": message}}, operation)


class _ContextNode:
    def __init__(self, context: dict[str, Any]) -> None:
        self.context = context

    def try_get_context(self, key: str) -> Any:
        return self.context.get(key)

    def get_all_context(self) -> dict[str, Any]:
        return self.context


class _ContextApp:
    def __init__(self, context: dict[str, Any]) -> None:
        self.node = _ContextNode(context)


@pytest.fixture
def config_loader() -> Any:
    from gco.config.config_loader import ConfigLoader

    return ConfigLoader(_ContextApp({}))


@pytest.fixture
def stack_manager(tmp_path: Path) -> Any:
    from cli.stacks import StackManager

    config = MagicMock()
    config.project_name = "gco"
    config.global_region = "us-east-2"
    config.api_gateway_region = "us-east-2"
    config.monitoring_region = "us-east-2"
    config.regions = ["us-east-1", "us-west-2"]
    return StackManager(config, project_root=tmp_path)


# ---------------------------------------------------------------------------
# gco/config/config_loader.py
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "project_name",
    [None, "GC0", "a", "1project", "project_name", "project.name"],
)
def test_project_name_validation_rejects_non_dns_prefixes(
    config_loader: Any,
    project_name: object,
) -> None:
    from gco.config.config_loader import ConfigValidationError

    config_loader.app = _ContextApp({"project_name": project_name})
    with pytest.raises(ConfigValidationError, match="Invalid project_name"):
        config_loader._validate_project_name()


def _valid_backend_tls() -> dict[str, int]:
    return {
        "root_generation": 1,
        "root_validity_days": 3650,
        "root_rotate_before_days": 180,
        "root_activation_delay_hours": 24,
        "root_overlap_days": 45,
        "leaf_validity_days": 30,
        "leaf_rotate_before_days": 10,
        "rotation_schedule_hours": 12,
        "trust_cache_ttl_seconds": 300,
        "trust_cache_max_stale_seconds": 3600,
    }


@pytest.mark.parametrize(
    ("updates", "message"),
    [
        ({"root_generation": 0}, "root_generation"),
        ({"root_validity_days": True}, "root_validity_days"),
        ({"root_rotate_before_days": 3650}, "less than root_validity_days"),
        ({"leaf_rotate_before_days": 30}, "less than leaf_validity_days"),
        ({"root_validity_days": 365, "leaf_validity_days": 365}, "must exceed leaf_validity_days"),
        ({"root_overlap_days": 30}, "must exceed leaf_validity_days"),
        ({"trust_cache_max_stale_seconds": 299}, "at least trust_cache_ttl_seconds"),
        (
            {"root_activation_delay_hours": 1, "trust_cache_max_stale_seconds": 3600},
            "must exceed the maximum stale trust cache window",
        ),
    ],
)
def test_backend_tls_validation_relations(
    config_loader: Any,
    updates: dict[str, int | bool],
    message: str,
) -> None:
    from gco.config.config_loader import ConfigValidationError

    value = {**_valid_backend_tls(), **updates}
    with (
        patch.object(config_loader, "get_backend_tls_config", return_value=value),
        pytest.raises(ConfigValidationError, match=message),
    ):
        config_loader._validate_backend_tls_config()


def test_regional_api_enabled_requires_boolean(config_loader: Any) -> None:
    from gco.config.config_loader import ConfigValidationError

    config_loader.app = _ContextApp(
        {
            "api_gateway": {
                "throttle_rate_limit": 10,
                "throttle_burst_limit": 20,
                "log_level": "INFO",
                "metrics_enabled": True,
                "tracing_enabled": True,
                "regional_api_enabled": "yes",
            }
        }
    )
    with pytest.raises(ConfigValidationError, match="regional_api_enabled"):
        config_loader._validate_api_gateway_config()


def test_capacity_history_checks_every_enabled_region(config_loader: Any) -> None:
    from gco.config.config_loader import ConfigValidationError

    config_loader.app = _ContextApp(
        {"historical": {"enabled_regions": ["us-east-1", "not-a-region"]}}
    )
    with pytest.raises(ConfigValidationError, match="not-a-region"):
        config_loader._validate_capacity_history_config()


@pytest.mark.parametrize(
    ("key", "getter", "message"),
    [
        ("global_accelerator", "get_global_accelerator_config", "must be a mapping"),
        ("backend_tls", "get_backend_tls_config", "must be a mapping"),
    ],
)
def test_mapping_getters_reject_truthy_non_mappings(
    config_loader: Any,
    key: str,
    getter: str,
    message: str,
) -> None:
    from gco.config.config_loader import ConfigValidationError

    config_loader.app = _ContextApp({key: ["not", "a", "mapping"]})
    with pytest.raises(ConfigValidationError, match=message):
        getattr(config_loader, getter)()


@pytest.mark.parametrize(
    ("updates", "message"),
    [
        ({"central_queue_worker_enabled": "yes"}, "must be a boolean"),
        ({"central_queue_poll_interval_seconds": 0}, "poll_interval_seconds"),
        ({"central_queue_batch_size": True}, "batch_size"),
        ({"central_queue_reconcile_limit": 501}, "reconcile_limit"),
        ({"central_queue_lease_seconds": 29}, "lease_seconds"),
        ({"central_queue_lease_renewal_seconds": 301}, "lease_renewal_seconds"),
        (
            {"central_queue_lease_seconds": 30, "central_queue_lease_renewal_seconds": 16},
            "no more than half",
        ),
    ],
)
def test_manifest_processor_worker_validation(
    config_loader: Any,
    updates: dict[str, Any],
    message: str,
) -> None:
    from gco.config.config_loader import ConfigValidationError

    config_loader.app = _ContextApp({"manifest_processor": updates})
    with pytest.raises(ConfigValidationError, match=message):
        config_loader.get_manifest_processor_config()


def test_fsx_partial_regional_node_group_preserves_inherited_fields() -> None:
    """A regional node-group patch must not erase global/default settings."""
    from gco.config.config_loader import ConfigLoader

    loader = ConfigLoader(
        _ContextApp(
            {
                "fsx_lustre": {
                    "enabled": True,
                    "node_group": {
                        "instance_types": ["m6i.large"],
                        "min_size": 2,
                        "capacity_type": "SPOT",
                        "labels": {"workload": "fsx"},
                    },
                },
                "fsx_lustre_regions": {"eu-west-1": {"node_group": {"max_size": 4}}},
            }
        )
    )

    node_group = loader.get_fsx_lustre_config("eu-west-1")["node_group"]
    assert node_group == {
        "instance_types": ["m6i.large"],
        "min_size": 2,
        "max_size": 4,
        "desired_size": 1,
        "ami_type": "AL2023_X86_64_STANDARD",
        "capacity_type": "SPOT",
        "disk_size": 100,
        "labels": {"workload": "fsx"},
    }


@pytest.mark.parametrize(
    ("context", "expected_node_group"),
    [
        ({"fsx_lustre": {"node_group": "invalid"}}, "invalid"),
        ({"fsx_lustre_regions": ["invalid"]}, None),
        ({"fsx_lustre_regions": {"us-east-1": "invalid"}}, None),
        (
            {
                "fsx_lustre": {"node_group": "invalid"},
                "fsx_lustre_regions": {"us-east-1": {"node_group": {"max_size": 2}}},
            },
            {"max_size": 2},
        ),
        (
            {"fsx_lustre_regions": {"us-east-1": {"node_group": "invalid"}}},
            "invalid",
        ),
    ],
)
def test_fsx_non_mapping_overrides_have_explicit_fallback_behavior(
    context: dict[str, Any],
    expected_node_group: object,
) -> None:
    from gco.config.config_loader import ConfigLoader

    result = ConfigLoader(_ContextApp(context)).get_fsx_lustre_config("us-east-1")
    node_group = result["node_group"]
    if expected_node_group is None:
        assert node_group["ami_type"] == "AL2023_X86_64_STANDARD"
        assert node_group["min_size"] == 0
    elif isinstance(expected_node_group, dict):
        assert node_group["max_size"] == expected_node_group["max_size"]
        assert node_group["ami_type"] == "AL2023_X86_64_STANDARD"
    else:
        assert node_group == expected_node_group


# ---------------------------------------------------------------------------
# gco/stacks/constants.py, nag_suppressions.py, policy helper
# ---------------------------------------------------------------------------


def test_cloudformation_partition_metadata_rejects_duplicate_region() -> None:
    from gco.stacks import constants

    session = MagicMock()
    session.get_available_partitions.return_value = ["aws", "aws-us-gov"]
    session.get_available_regions.side_effect = [["shared-1"], ["shared-1"]]
    constants.cloudformation_region_partitions.cache_clear()
    try:
        with (
            patch("boto3.Session", return_value=session),
            pytest.raises(RuntimeError, match="both 'aws' and 'aws-us-gov'"),
        ):
            constants.cloudformation_region_partitions()
    finally:
        constants.cloudformation_region_partitions.cache_clear()


def test_cloudformation_partition_metadata_rejects_empty_catalog() -> None:
    from gco.stacks import constants

    session = MagicMock()
    session.get_available_partitions.return_value = ["aws"]
    session.get_available_regions.return_value = []
    constants.cloudformation_region_partitions.cache_clear()
    try:
        with (
            patch("boto3.Session", return_value=session),
            pytest.raises(RuntimeError, match="contains no CloudFormation regions"),
        ):
            constants.cloudformation_region_partitions()
    finally:
        constants.cloudformation_region_partitions.cache_clear()


@pytest.mark.parametrize(
    ("function", "args", "kwargs", "exception"),
    [
        ("validated_deployment_partition", ([],), {}, ValueError),
        (
            "validated_deployment_partition",
            (["us-east-1"],),
            {"region_partitions": {}},
            RuntimeError,
        ),
        (
            "validated_regional_deployment_regions",
            (["us-east-1"],),
            {"known_regions": []},
            RuntimeError,
        ),
    ],
)
def test_region_contract_rejects_missing_topology_metadata(
    function: str,
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
    exception: type[Exception],
) -> None:
    from gco.stacks import constants

    with pytest.raises(exception):
        getattr(constants, function)(*args, **kwargs)


def test_nag_normalization_rejects_non_string_detail() -> None:
    from gco.stacks import nag_suppressions as nag

    with pytest.raises(ValueError, match="expected an exact detail string"):
        nag._normalize({"id": "Rule", "reason": "because", "appliesTo": [123]})


def test_nag_acknowledgment_rejects_unresolved_whole_detail() -> None:
    from gco.stacks import nag_suppressions as nag

    scope = MagicMock()
    stack = SimpleNamespace(partition="aws", account="123456789012", region="us-east-1")
    with (
        patch.object(nag.Stack, "of", return_value=stack),
        patch.object(nag.Token, "is_unresolved", side_effect=lambda value: value == "token"),
        pytest.raises(ValueError, match="contains an unresolved token"),
    ):
        nag.acknowledge_nag_findings(
            scope,
            [nag.NagSuppression(id="Rule", reason="because", applies_to=["token"])],
        )
    scope.node.add_metadata.assert_not_called()


def test_nag_acknowledgment_expands_resolved_environment_variants() -> None:
    from gco.stacks import nag_suppressions as nag

    scope = MagicMock()
    stack = SimpleNamespace(partition="aws", account="123456789012", region="us-east-1")
    detail = "Resource::arn:<AWS::Partition>:service:<AWS::Region>:<AWS::AccountId>:thing"
    with (
        patch.object(nag.Stack, "of", return_value=stack),
        patch.object(nag.Token, "is_unresolved", return_value=False),
    ):
        nag.acknowledge_nag_findings(
            scope,
            [nag.NagSuppression(id="Rule", reason="because", applies_to=[detail])],
        )
    metadata = scope.node.add_metadata.call_args.args[1]
    assert f"Rule[{detail}]" in metadata
    assert "Rule[Resource::arn:aws:service:us-east-1:123456789012:thing]" in metadata


def test_empty_nag_acknowledgments_add_no_metadata() -> None:
    from gco.stacks import nag_suppressions as nag

    scope = MagicMock()
    stack = SimpleNamespace(partition="aws", account="123", region="us-east-1")
    with patch.object(nag.Stack, "of", return_value=stack):
        nag.acknowledge_nag_findings(scope, [])
    scope.node.add_metadata.assert_not_called()


def test_nag_plugins_include_all_five_rule_packs() -> None:
    from gco.stacks import nag_suppressions as nag

    constructors = [
        "AwsSolutionsChecks",
        "HIPAASecurityChecks",
        "NIST80053R5Checks",
        "PCIDSS321Checks",
        "ServerlessChecks",
    ]
    patches = [patch.object(nag, name, return_value=name) for name in constructors]
    started = [item.start() for item in patches]
    try:
        assert nag.nag_validation_plugins("scope", verbose=False) == constructors
        for mock_constructor in started:
            mock_constructor.assert_called_once_with("scope", verbose=False)
    finally:
        for item in reversed(patches):
            item.stop()


def test_unknown_nag_stack_type_applies_only_common_suppressions() -> None:
    from gco.stacks import nag_suppressions as nag

    with (
        patch.object(nag, "add_lambda_suppressions") as add_lambda,
        patch.object(nag, "add_iam_suppressions") as add_iam,
        patch.object(nag, "add_eks_suppressions") as add_eks,
        patch.object(nag, "add_backup_suppressions") as add_backup,
        patch.object(nag, "add_api_gateway_suppressions") as add_api,
        patch.object(nag, "add_monitoring_suppressions") as add_monitoring,
        patch.object(nag, "add_storage_suppressions") as add_storage,
    ):
        nag.apply_all_suppressions("stack", stack_type="future-stack")
    add_lambda.assert_called_once_with("stack")
    add_iam.assert_called_once()
    for helper in (add_eks, add_backup, add_api, add_monitoring, add_storage):
        helper.assert_not_called()


@pytest.mark.parametrize("partition", ["", "aws:evil"])
def test_load_balancer_policy_rejects_invalid_partition(partition: str) -> None:
    from gco.stacks.aws_load_balancer_controller_policy import (
        aws_load_balancer_controller_policy_document,
    )

    with pytest.raises(ValueError, match="Invalid AWS partition"):
        aws_load_balancer_controller_policy_document(partition)


def test_load_balancer_policy_returns_fresh_partition_specific_documents() -> None:
    from gco.stacks.aws_load_balancer_controller_policy import (
        aws_load_balancer_controller_policy_document,
    )

    first = aws_load_balancer_controller_policy_document("aws-us-gov")
    second = aws_load_balancer_controller_policy_document("aws-us-gov")
    assert first == second
    assert first is not second
    assert "arn:aws-us-gov:" in json.dumps(first)


# ---------------------------------------------------------------------------
# gco_mcp/tools/stacks.py -- isolated import-time registry and option matrices
# ---------------------------------------------------------------------------


_MCP_IDS = itertools.count()
_MCP_LONG_RESULT = '{"task":"ok"}'
_FLAG_DEPLOY = "GCO_ENABLE_INFRASTRUCTURE_DEPLOY"
_FLAG_DESTROY = "GCO_ENABLE_INFRASTRUCTURE_DESTROY"


class _McpStub:
    def tool(self, **_kwargs: Any) -> Any:
        return lambda function: function


class _InjectedDependency:
    pass


class _TaskConfig:
    def __init__(self, mode: str) -> None:
        self.mode = mode


def _stub_module(name: str, **attributes: Any) -> types.ModuleType:
    module = types.ModuleType(name)
    for key, value in attributes.items():
        setattr(module, key, value)
    return module


@dataclass
class _LoadedMcpStacks:
    module: types.ModuleType
    run_cli: Mock
    long_task: AsyncMock
    load_cdk_json: Mock


@contextmanager
def _isolated_mcp_stacks(*enabled_flags: str) -> Iterator[_LoadedMcpStacks]:
    enabled = frozenset(enabled_flags)
    run_cli = Mock(return_value='{"cli":"ok"}')
    long_task = AsyncMock(return_value=_MCP_LONG_RESULT)
    load_cdk_json = Mock(return_value={"regional": ["us-east-1", "us-west-2"]})

    feature_flags = _stub_module(
        "feature_flags",
        FLAG_CAPACITY_PURCHASE="purchase",
        FLAG_CONFIG_MANAGEMENT="config",
        FLAG_DESTRUCTIVE_OPERATIONS="destructive",
        FLAG_IMAGE_PUBLISH="images",
        FLAG_INFRASTRUCTURE_DEPLOY=_FLAG_DEPLOY,
        FLAG_INFRASTRUCTURE_DESTROY=_FLAG_DESTROY,
        FLAG_MODEL_UPLOAD="models",
        is_enabled=lambda flag: flag in enabled,
    )
    tools_package = _stub_module("tools")
    tools_package.__path__ = []  # type: ignore[attr-defined]
    fastmcp_package = _stub_module("fastmcp")
    fastmcp_package.__path__ = []  # type: ignore[attr-defined]
    fastmcp_server = _stub_module("fastmcp.server")
    fastmcp_server.__path__ = []  # type: ignore[attr-defined]
    fastmcp_utilities = _stub_module("fastmcp.utilities")
    fastmcp_utilities.__path__ = []  # type: ignore[attr-defined]
    cli_package = _stub_module("cli")
    cli_package.__path__ = []  # type: ignore[attr-defined]

    stubs = {
        "audit": _stub_module("audit", audit_logged=lambda function: function),
        "cli": cli_package,
        "cli.config": _stub_module("cli.config", _load_cdk_json=load_cdk_json),
        "cli_runner": _stub_module("cli_runner", _run_cli=run_cli),
        "fastmcp": fastmcp_package,
        "fastmcp.server": fastmcp_server,
        "fastmcp.server.dependencies": _stub_module(
            "fastmcp.server.dependencies",
            CurrentContext=_InjectedDependency,
            Progress=_InjectedDependency,
            get_context=Mock(side_effect=LookupError),
        ),
        "fastmcp.utilities": fastmcp_utilities,
        "fastmcp.utilities.tasks": _stub_module("fastmcp.utilities.tasks", TaskConfig=_TaskConfig),
        "feature_flags": feature_flags,
        "local_data": _stub_module(
            "local_data", resolve_local_path=Mock(), stage_upload_path=Mock()
        ),
        "server": _stub_module("server", mcp=_McpStub()),
        "tools": tools_package,
        "tools._long_task": _stub_module("tools._long_task", _run_long_task=long_task),
        "tools._task_status": _stub_module(
            "tools._task_status", get_task=Mock(), list_tasks=Mock(return_value=[]), tail_log=Mock()
        ),
    }
    source = PROJECT_ROOT / "gco_mcp" / "tools" / "stacks.py"
    name = f"_coverage_100_mcp_stacks_{next(_MCP_IDS)}"
    spec = importlib.util.spec_from_file_location(name, source)
    assert spec is not None and spec.loader is not None
    with patch.dict(sys.modules, stubs, clear=False):
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
        yield _LoadedMcpStacks(module, run_cli, long_task, load_cdk_json)
    sys.modules.pop(name, None)


def test_mcp_stack_count_handles_import_failure() -> None:
    with _isolated_mcp_stacks() as loaded:
        original_import = builtins.__import__

        def fail_cli_config(name: str, *args: Any, **kwargs: Any) -> Any:
            if name == "cli.config":
                raise ImportError("cli unavailable")
            return original_import(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=fail_cli_config):
            assert loaded.module._expected_stack_count_for_all() is None


def test_mcp_addons_status_explicit_region() -> None:
    with _isolated_mcp_stacks() as loaded:
        result = asyncio.run(loaded.module.addons_status(region="eu-west-1"))
    assert result == '{"cli":"ok"}'
    loaded.run_cli.assert_called_once_with("stacks", "addons", "status", "-r", "eu-west-1")


@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [
        (
            {"yes": False, "outputs_file": None, "tags": None, "parallel": False},
            ["gco", "stacks", "deploy-all"],
        ),
        (
            {
                "yes": True,
                "outputs_file": "outputs.json",
                "tags": ["A=1", "B=two"],
                "parallel": True,
                "max_workers": 0,
            },
            [
                "gco",
                "stacks",
                "deploy-all",
                "-y",
                "--outputs-file",
                "outputs.json",
                "--tag",
                "A=1",
                "--tag",
                "B=two",
                "--parallel",
                "--max-workers",
                "0",
            ],
        ),
    ],
)
def test_mcp_deploy_all_option_matrix(kwargs: dict[str, Any], expected: list[str]) -> None:
    with _isolated_mcp_stacks(_FLAG_DEPLOY) as loaded:
        assert asyncio.run(loaded.module.deploy_all(**kwargs)) == _MCP_LONG_RESULT
        call_kwargs = loaded.long_task.await_args.kwargs
        assert loaded.long_task.await_args.args[0] == expected
        assert call_kwargs["is_stack_op"] is True
        assert call_kwargs["total_units"] == 5


@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [
        (
            {
                "yes": False,
                "parallel": False,
                "max_workers": None,
                "retain_volumes": False,
            },
            ["gco", "stacks", "destroy-all"],
        ),
        (
            {
                "yes": True,
                "parallel": True,
                "max_workers": 3,
                "retain_volumes": True,
            },
            [
                "gco",
                "stacks",
                "destroy-all",
                "-y",
                "--parallel",
                "--max-workers",
                "3",
                "--retain-volumes",
            ],
        ),
    ],
)
def test_mcp_destroy_all_option_matrix(kwargs: dict[str, Any], expected: list[str]) -> None:
    with _isolated_mcp_stacks(_FLAG_DESTROY) as loaded:
        assert asyncio.run(loaded.module.destroy_all(**kwargs)) == _MCP_LONG_RESULT
        assert loaded.long_task.await_args.args[0] == expected
        assert loaded.long_task.await_args.kwargs["total_units"] == 5


# ---------------------------------------------------------------------------
# cli/commands/stacks_cmd.py
# ---------------------------------------------------------------------------


def _cli_config(default_region: str | None = "us-east-1") -> Any:
    from cli.config import GCOConfig

    return GCOConfig(
        project_name="test-gco",
        default_region=default_region,
        output_format="table",
        verbose=False,
        use_regional_api=False,
    )


def _invoke_stacks(
    args: list[str],
    *,
    config: Any | None = None,
    input_text: str | None = None,
) -> Any:
    from cli.commands.stacks_cmd import stacks

    kwargs: dict[str, Any] = {"obj": config or _cli_config()}
    if input_text is not None:
        kwargs["input"] = input_text
    return CliRunner().invoke(stacks, args, **kwargs)


def test_setup_access_uses_default_region_and_tolerates_existing_access() -> None:
    commands: list[list[str]] = []

    def run(argv: list[str], **_kwargs: Any) -> Any:
        commands.append(argv)
        if argv[:3] == ["aws", "eks", "describe-cluster"]:
            return SimpleNamespace(stdout='{"public": false, "private": true}', returncode=0)
        if argv[:3] == ["aws", "sts", "get-caller-identity"]:
            query = argv[argv.index("--query") + 1]
            value = (
                "arn:aws:sts::123456789012:assumed-role/Admin/test-session\n"
                if query == "Arn"
                else "123456789012\n"
            )
            return SimpleNamespace(stdout=value, returncode=0)
        if "create-access-entry" in argv or "associate-access-policy" in argv:
            raise subprocess.CalledProcessError(1, argv, stderr="already exists")
        if argv[0] == "kubectl":
            return SimpleNamespace(returncode=1, stdout="", stderr="i/o timeout")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    with (
        patch("cli.config._load_cdk_json", return_value={}),
        patch("subprocess.run", side_effect=run),
        patch("time.sleep"),
    ):
        result = _invoke_stacks(["access"], config=_cli_config("eu-central-1"))

    assert result.exit_code == 0, result.output
    assert "test-gco-eu-central-1" in result.output
    assert "private-only cluster" in result.output
    assert "Access entry may already exist" in result.output
    assert "Policy may already be associated" in result.output
    assert any("arn:aws:iam::123456789012:role/Admin" in command for command in commands)


def test_setup_access_malformed_assumed_role_keeps_original_principal() -> None:
    def run(argv: list[str], **_kwargs: Any) -> Any:
        if argv[:3] == ["aws", "eks", "describe-cluster"]:
            return SimpleNamespace(stdout='{"public": true, "publicCidrs": ["10.0.0.0/8"]}')
        if argv[:3] == ["aws", "sts", "get-caller-identity"]:
            return SimpleNamespace(
                stdout="arn:aws:sts::123456789012:assumed-role/no-session\n",
                returncode=0,
            )
        if argv[0] == "kubectl":
            return SimpleNamespace(returncode=1, stdout="", stderr="authorization failed")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    with (
        patch("subprocess.run", side_effect=run) as mocked,
        patch("time.sleep"),
    ):
        result = _invoke_stacks(["access", "-r", "us-east-1"])
    assert result.exit_code == 0, result.output
    assert "CIDR allowlist" in result.output
    assert "connected but no nodes found" in result.output
    assert not any(
        command.args[0][:3] == ["aws", "sts", "get-caller-identity"]
        and "Account" in command.args[0]
        for command in mocked.call_args_list
    )


@pytest.mark.parametrize(
    ("exception", "expected"),
    [
        (subprocess.CalledProcessError(2, "aws", stderr="denied"), "Command failed: denied"),
        (FileNotFoundError("aws"), "Required tool not found"),
        (RuntimeError("unexpected"), "Failed to set up access: unexpected"),
    ],
)
def test_setup_access_reports_outer_failures(exception: Exception, expected: str) -> None:
    calls = 0

    def run(_argv: list[str], **_kwargs: Any) -> Any:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise subprocess.CalledProcessError(1, "describe")
        raise exception

    with patch("subprocess.run", side_effect=run):
        result = _invoke_stacks(["access", "-r", "us-east-1"])
    assert result.exit_code == 1
    assert expected in result.output


def _report(changed: bool) -> Any:
    return SimpleNamespace(changed=changed, summary=lambda: "managed summary")


@pytest.mark.parametrize(
    ("args", "patch_target"),
    [
        (["regions", "list"], "cli.managed_config.get_deployment_regions_status"),
        (
            ["regions", "add", "us-west-2", "-y"],
            "cli.managed_config.add_deployment_region",
        ),
        (
            ["regions", "remove", "us-west-2", "-y"],
            "cli.managed_config.remove_deployment_region",
        ),
        (
            ["regions", "set", "monitoring", "us-west-2", "-y"],
            "cli.managed_config.set_deployment_region_role",
        ),
        (["bedrock", "show"], "cli.managed_config.get_bedrock_model_status"),
        (
            ["bedrock", "set-mission-model", "model", "-y"],
            "cli.managed_config.set_mission_default_model",
        ),
        (
            ["bedrock", "set-capacity-advisor-model", "model", "-y"],
            "cli.managed_config.set_capacity_advisor_default_model",
        ),
        (
            ["bedrock", "set-claude-code-model", "model", "-y"],
            "cli.managed_config.set_claude_code_default_model",
        ),
        (
            ["bedrock", "set-codex-model", "model", "-y"],
            "cli.managed_config.set_codex_default_model",
        ),
        (
            ["bedrock", "set-codex-reasoning-effort", "high", "-y"],
            "cli.managed_config.set_codex_reasoning_effort",
        ),
    ],
)
def test_managed_stack_commands_report_errors(args: list[str], patch_target: str) -> None:
    from cli.managed_config import ManagedConfigError

    with patch(patch_target, side_effect=ManagedConfigError("invalid managed config")):
        result = _invoke_stacks(args)
    assert result.exit_code == 1
    assert "invalid managed config" in result.output


@pytest.mark.parametrize(
    ("args", "patch_target"),
    [
        (
            ["regions", "add", "us-west-2", "-y"],
            "cli.managed_config.add_deployment_region",
        ),
        (
            ["regions", "remove", "us-west-2", "-y"],
            "cli.managed_config.remove_deployment_region",
        ),
        (
            ["regions", "set", "monitoring", "us-west-2", "-y"],
            "cli.managed_config.set_deployment_region_role",
        ),
        (
            ["bedrock", "set-mission-model", "model", "-y"],
            "cli.managed_config.set_mission_default_model",
        ),
        (
            ["bedrock", "set-capacity-advisor-model", "model", "-y"],
            "cli.managed_config.set_capacity_advisor_default_model",
        ),
        (
            ["bedrock", "set-claude-code-model", "model", "-y"],
            "cli.managed_config.set_claude_code_default_model",
        ),
        (
            ["bedrock", "set-codex-model", "model", "-y"],
            "cli.managed_config.set_codex_default_model",
        ),
        (
            ["bedrock", "set-codex-reasoning-effort", "high", "-y"],
            "cli.managed_config.set_codex_reasoning_effort",
        ),
    ],
)
def test_managed_stack_commands_report_noop(args: list[str], patch_target: str) -> None:
    with patch(patch_target, return_value=_report(False)):
        result = _invoke_stacks(args)
    assert result.exit_code == 0, result.output
    assert "managed summary" in result.output


def test_fsx_enable_confirmation_omits_unset_paths() -> None:
    with patch("cli.stacks.update_fsx_config") as update:
        result = _invoke_stacks(["fsx", "enable"], input_text="y\n")
    assert result.exit_code == 0, result.output
    assert "Import Path" not in result.output
    assert "Export Path" not in result.output
    update.assert_called_once()
    assert update.call_args.args[0]["auto_import_policy"] is None


def test_project_name_reads_context_and_falls_back(tmp_path: Path, monkeypatch: Any) -> None:
    from cli.commands import stacks_cmd

    monkeypatch.chdir(tmp_path)
    (tmp_path / "cdk.json").write_text(
        json.dumps({"context": {"project_name": "custom"}}), encoding="utf-8"
    )
    assert stacks_cmd._project_name() == "custom"
    (tmp_path / "cdk.json").write_text("not-json", encoding="utf-8")
    assert stacks_cmd._project_name() == "gco"
    (tmp_path / "cdk.json").unlink()
    assert stacks_cmd._project_name() == "gco"


def test_target_regions_uses_final_fallback() -> None:
    from cli.commands import stacks_cmd

    with patch.object(stacks_cmd, "_load_cdk_json", return_value={}):
        assert stacks_cmd._target_regions(_cli_config(None), None, False) == ["us-east-1"]


def test_addons_status_handles_client_failure() -> None:
    from cli.commands import stacks_cmd

    formatter = MagicMock()
    with patch("boto3.client", side_effect=RuntimeError("ssm unavailable")):
        stacks_cmd._addons_status_one(formatter, "gco", "us-east-1")
    formatter.print_error.assert_called_once()
    assert "ssm unavailable" in formatter.print_error.call_args.args[0]


def test_addons_status_formats_input_invalid_empty_and_success_rows() -> None:
    from cli.commands import stacks_cmd

    paginator = MagicMock()
    paginator.paginate.return_value = [
        {
            "Parameters": [
                {"Name": "/gco/addons/us-east-1/_input", "Value": "ignored"},
                {"Name": "/gco/addons/us-east-1/bad", "Value": "not-json"},
                {
                    "Name": "/gco/addons/us-east-1/keda",
                    "Value": json.dumps({"status": "installed", "message": "ready"}),
                },
                {
                    "Name": "/gco/addons/us-east-1/volcano",
                    "Value": json.dumps({"status": "failed", "message": "boom"}),
                },
            ]
        }
    ]
    client = MagicMock()
    client.get_paginator.return_value = paginator
    formatter = MagicMock()
    with patch("boto3.client", return_value=client):
        stacks_cmd._addons_status_one(formatter, "gco", "us-east-1")
    assert formatter.print_success.call_count == 1
    assert formatter.print_error.call_count == 2

    paginator.paginate.return_value = [{"Parameters": []}]
    formatter.reset_mock()
    with patch("boto3.client", return_value=client):
        stacks_cmd._addons_status_one(formatter, "gco", "us-east-1")
    assert "No add-on status" in formatter.print_info.call_args.args[0]


def test_decode_addon_replay_input_decodes_compressed_value() -> None:
    from cli.commands.stacks_cmd import _decode_addon_replay_input

    raw = '{"ClusterName":"gco-us-east-1"}'
    encoded = base64.b64encode(zlib.compress(raw.encode("utf-8"))).decode("ascii")
    assert _decode_addon_replay_input(encoded) == raw


def test_addons_install_fence_and_state_machine_paths() -> None:
    from cli.commands import stacks_cmd

    formatter = MagicMock()
    ssm = MagicMock()
    with patch("boto3.client", return_value=ssm):
        assert stacks_cmd._addons_install_one(formatter, "gco", "us-east-1") is False
    assert "teardown is active" in formatter.print_error.call_args.args[0]

    ssm.get_parameter.side_effect = [
        _client_error("ParameterNotFound"),
        {"Parameter": {"Value": '{"input":true}'}},
    ]
    sfn = MagicMock()
    sfn.list_state_machines.return_value = {"stateMachines": []}
    formatter.reset_mock()
    with patch("boto3.client", side_effect=[ssm, sfn]):
        assert stacks_cmd._addons_install_one(formatter, "gco", "us-east-1") is False
    assert "No HelmInstall state machine" in formatter.print_error.call_args.args[0]

    ssm.reset_mock()
    ssm.get_parameter.side_effect = [
        _client_error("ParameterNotFound"),
        {"Parameter": {"Value": '{"input":true}'}},
    ]
    sfn.list_state_machines.return_value = {
        "stateMachines": [{"name": "HelmInstallMachine", "stateMachineArn": "arn:machine"}]
    }
    sfn.start_execution.return_value = {"executionArn": "arn:execution"}
    formatter.reset_mock()
    with patch("boto3.client", side_effect=[ssm, sfn]):
        assert stacks_cmd._addons_install_one(formatter, "gco", "us-east-1") is True
    formatter.print_success.assert_called_once()
    assert "arn:execution" in formatter.print_info.call_args_list[0].args[0]


# ---------------------------------------------------------------------------
# cli/stacks.py -- asset, locking, process, and small manager contracts
# ---------------------------------------------------------------------------


def test_asset_tree_paths_selects_nested_entries_and_rejects_missing(tmp_path: Path) -> None:
    import cli.stacks as stacks

    nested = tmp_path / "selected" / "nested"
    nested.mkdir(parents=True)
    (nested / "payload.txt").write_text("payload", encoding="utf-8")
    paths = list(stacks._asset_tree_paths(tmp_path, ("selected",)))
    assert [path.relative_to(tmp_path).as_posix() for path in paths] == [
        "selected",
        "selected/nested",
        "selected/nested/payload.txt",
    ]
    with pytest.raises(FileNotFoundError):
        list(stacks._asset_tree_paths(tmp_path, ("missing",)))


def test_asset_tree_digest_handles_entries_and_failures(tmp_path: Path) -> None:
    import cli.stacks as stacks

    (tmp_path / "directory").mkdir()
    (tmp_path / "file.txt").write_text("content", encoding="utf-8")
    (tmp_path / "link").symlink_to("file.txt")
    (tmp_path / "ignored.pyc").write_bytes(b"ignored")
    first = stacks._asset_tree_digest(tmp_path)
    assert first is not None
    (tmp_path / "file.txt").write_text("changed", encoding="utf-8")
    assert stacks._asset_tree_digest(tmp_path) != first

    with patch.object(stacks, "_asset_tree_paths", side_effect=UnicodeError("bad path")):
        assert stacks._asset_tree_digest(tmp_path) is None
    assert stacks._asset_tree_digest(tmp_path / "absent") is None


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFO requires POSIX")
def test_asset_tree_digest_rejects_unsupported_entry(tmp_path: Path) -> None:
    import cli.stacks as stacks

    os.mkfifo(tmp_path / "pipe")
    assert stacks._asset_tree_digest(tmp_path) is None


def test_write_build_manifest_refuses_unhashable_tree(tmp_path: Path) -> None:
    import cli.stacks as stacks

    with (
        patch.object(stacks, "_asset_tree_digest", return_value=None),
        pytest.raises(RuntimeError, match="Unable to hash completed Lambda asset"),
    ):
        stacks._write_build_manifest(tmp_path, "source")


def test_windows_lock_byte_preserves_nonempty_file() -> None:
    import cli.stacks as stacks

    handle = io.BytesIO(b"x")
    stacks._ensure_windows_lock_byte(handle)
    assert handle.getvalue() == b"x"
    assert handle.tell() == 0


@pytest.mark.parametrize("raw", ["invalid", "nan", "inf", "0", "-1"])
def test_file_lock_timeout_invalid_values_use_default(monkeypatch: Any, raw: str) -> None:
    import cli.stacks as stacks

    monkeypatch.setenv("GCO_ASSET_LOCK_TIMEOUT_SECONDS", raw)
    assert stacks._file_lock_timeout_seconds("asset") == stacks._ASSET_LOCK_TIMEOUT_SECONDS_DEFAULT


def test_posix_lock_propagates_non_contention_error() -> None:
    import cli.stacks as stacks

    with (
        patch("fcntl.flock", side_effect=OSError(errno.EBADF, "bad fd")),
        pytest.raises(OSError, match="bad fd"),
    ):
        stacks._acquire_posix_flock(1, lock_name="lock", exclusive=True, purpose="asset")


def test_posix_lock_contention_times_out(monkeypatch: Any) -> None:
    import cli.stacks as stacks

    monkeypatch.setenv("GCO_ASSET_LOCK_TIMEOUT_SECONDS", "0.01")
    with (
        patch("fcntl.flock", side_effect=BlockingIOError(errno.EAGAIN, "busy")),
        patch.object(stacks.time, "monotonic", side_effect=[0.0, 1.0, 1.0]),
        pytest.raises(TimeoutError, match="Timed out"),
    ):
        stacks._acquire_posix_flock(1, lock_name="lock", exclusive=True, purpose="asset")


def test_release_file_lock_windows_and_posix() -> None:
    import cli.stacks as stacks

    handle = MagicMock()
    handle.fileno.return_value = 7
    msvcrt = SimpleNamespace(LK_UNLCK=9, locking=MagicMock())
    with (
        patch.object(stacks.os, "name", "nt"),
        patch.dict(sys.modules, {"msvcrt": msvcrt}),
    ):
        stacks._release_file_lock(handle)
    msvcrt.locking.assert_called_once_with(7, 9, 1)

    with (
        patch.object(stacks.os, "name", "posix"),
        patch("fcntl.flock") as flock,
    ):
        stacks._release_file_lock(handle)
    flock.assert_called_once()


def test_lambda_asset_lock_nested_and_upgrade_contract(tmp_path: Path) -> None:
    import cli.stacks as stacks

    build = tmp_path / "lambda" / "demo-build"
    lock_path = build.with_name(f".{build.name}.lock")
    lock_key = os.path.normcase(os.path.abspath(lock_path))
    with (
        patch.object(stacks, "_acquire_file_lock") as acquire,
        patch.object(stacks, "_release_file_lock") as release,
    ):
        with stacks._lambda_asset_lock(build, exclusive=True):
            with stacks._lambda_asset_lock(build, exclusive=False):
                assert stacks._thread_asset_locks()[lock_key] == (True, 2)
            assert stacks._thread_asset_locks()[lock_key] == (True, 1)
        assert lock_key not in stacks._thread_asset_locks()
    acquire.assert_called_once()
    release.assert_called_once()

    with (
        patch.object(stacks, "_acquire_file_lock"),
        patch.object(stacks, "_release_file_lock"),
        stacks._lambda_asset_lock(build, exclusive=False),
        pytest.raises(RuntimeError, match="Cannot upgrade shared"),
        stacks._lambda_asset_lock(build, exclusive=True),
    ):
        pass


def test_recover_interrupted_asset_publish_restores_oldest_fallback(tmp_path: Path) -> None:
    import cli.stacks as stacks

    parent = tmp_path / "lambda"
    parent.mkdir()
    build = parent / "demo-build"
    backup = parent / ".demo-build.backup-one"
    staging = parent / ".demo-build.staging-one"
    backup.mkdir()
    staging.mkdir()
    (backup / "payload").write_text("old", encoding="utf-8")
    with patch.object(Path, "stat", side_effect=OSError("unreadable timestamp")):
        stacks._recover_interrupted_asset_publish(build)
    assert (build / "payload").read_text(encoding="utf-8") == "old"
    assert not staging.exists()


def test_publish_staged_asset_rolls_back_previous_tree(tmp_path: Path) -> None:
    import cli.stacks as stacks

    parent = tmp_path / "lambda"
    parent.mkdir()
    build = parent / "demo-build"
    staging = parent / ".demo-build.staging-test"
    build.mkdir()
    staging.mkdir()
    (build / "payload").write_text("old", encoding="utf-8")
    (staging / "payload").write_text("new", encoding="utf-8")
    real_replace = os.replace
    calls = 0

    def replace(source: Any, target: Any) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("publish failed")
        real_replace(source, target)

    with (
        patch.object(stacks.os, "replace", side_effect=replace),
        pytest.raises(OSError, match="publish failed"),
    ):
        stacks._publish_staged_asset(staging, build)
    assert (build / "payload").read_text(encoding="utf-8") == "old"


def test_prepare_lambda_asset_rechecks_and_cleans_staging(tmp_path: Path) -> None:
    import cli.stacks as stacks

    source = tmp_path / "lambda" / "source"
    build = tmp_path / "lambda" / "source-build"
    source.mkdir(parents=True)
    (source / "handler.py").write_text("source", encoding="utf-8")

    def builder(staging: Path) -> None:
        (staging / "handler.py").write_text("built", encoding="utf-8")

    assert (
        stacks._prepare_lambda_asset(
            source,
            build,
            source_inputs=None,
            display_name="demo",
            builder=builder,
        )
        is True
    )
    assert (build / "handler.py").read_text(encoding="utf-8") == "built"

    with (
        patch.object(stacks, "_asset_build_is_fresh", return_value=False),
        patch.object(stacks, "_asset_tree_digest", return_value=None),
        pytest.raises(RuntimeError, match="incomplete or unreadable"),
    ):
        stacks._prepare_lambda_asset(
            source,
            build,
            source_inputs=None,
            display_name="demo",
            builder=builder,
        )


def test_prepare_lambda_asset_detects_source_change(tmp_path: Path) -> None:
    import cli.stacks as stacks

    source = tmp_path / "lambda" / "source"
    build = tmp_path / "lambda" / "source-build"
    source.mkdir(parents=True)
    payload = source / "handler.py"
    payload.write_text("before", encoding="utf-8")

    def builder(staging: Path) -> None:
        (staging / "handler.py").write_text("built", encoding="utf-8")
        payload.write_text("after", encoding="utf-8")

    with pytest.raises(RuntimeError, match="sources changed while packaging"):
        stacks._prepare_lambda_asset(
            source,
            build,
            source_inputs=None,
            display_name="demo",
            builder=builder,
        )
    assert not list(build.parent.glob(".source-build.staging-*"))


def test_atomic_write_bytes_applies_requested_mode(tmp_path: Path) -> None:
    import cli.stacks as stacks

    target = tmp_path / "cdk.json"
    stacks._atomic_write_bytes(target, b"{}", mode=0o640)
    assert target.read_bytes() == b"{}"
    assert stat.S_IMODE(target.stat().st_mode) == 0o640


def test_config_process_lock_wraps_posix_open_failure(tmp_path: Path) -> None:
    import cli.stacks as stacks

    with (
        patch.object(stacks.os, "open", side_effect=OSError("denied")),
        pytest.raises(stacks.ConfigMutationLockError, match="could not lock"),
        stacks._config_process_lock(tmp_path),
    ):
        pass


def test_known_cloudformation_regions_delegates() -> None:
    import cli.stacks as stacks

    stacks._known_cloudformation_regions.cache_clear()
    try:
        with patch.object(
            stacks, "known_cloudformation_regions", return_value=frozenset({"test-1"})
        ) as known:
            assert stacks._known_cloudformation_regions() == frozenset({"test-1"})
        known.assert_called_once_with()
    finally:
        stacks._known_cloudformation_regions.cache_clear()


def test_safe_rmtree_uses_validated_fallback(tmp_path: Path) -> None:
    import cli.stacks as stacks

    target = tmp_path / "lambda" / "demo-build"
    target.mkdir(parents=True)
    with (
        patch.object(stacks.shutil, "rmtree", side_effect=OSError("busy")),
        patch.object(stacks.subprocess, "run") as run,
    ):
        stacks._safe_rmtree(target)
    run.assert_called_once_with(["rm", "-rf", "--", str(target.resolve())], check=True)


def test_container_runtime_none_result_is_cached() -> None:
    import cli.stacks as stacks

    stacks._container_runtime_checked = False
    stacks._container_runtime_cache = None
    with patch.object(stacks, "_detect_container_runtime_uncached", return_value=None) as probe:
        assert stacks._detect_container_runtime() is None
        assert stacks._detect_container_runtime() is None
    probe.assert_called_once_with()


def test_find_cdk_falls_back_to_common_global_path(stack_manager: Any) -> None:
    with (
        patch.object(Path, "is_file", return_value=False),
        patch("subprocess.run", side_effect=subprocess.CalledProcessError(1, "which")),
        patch("os.path.exists", side_effect=lambda path: path == "/usr/local/bin/cdk"),
    ):
        assert stack_manager._find_cdk() == "/usr/local/bin/cdk"


def test_diagnose_deploy_failure_is_best_effort(stack_manager: Any) -> None:
    stack_manager._get_deploy_region = MagicMock(return_value="us-east-1")
    with patch("boto3.client", side_effect=RuntimeError("offline")):
        stack_manager._diagnose_deploy_failure("gco-global")


def test_sync_and_rebuild_lambda_packages_are_idempotent(
    stack_manager: Any,
    tmp_path: Path,
) -> None:
    import cli.stacks as stacks

    source = tmp_path / "shared.py"
    target = tmp_path / "target" / "shared.py"
    source.write_text("value = 1\n", encoding="utf-8")
    target.parent.mkdir()
    mapping = {str(source.relative_to(tmp_path)): (str(target.relative_to(tmp_path)),)}
    with patch.object(stacks, "LAMBDA_SHARED_SOURCE_TARGETS", mapping):
        stack_manager._sync_lambda_sources()
        stack_manager._sync_lambda_sources()
    assert target.read_text(encoding="utf-8") == "value = 1\n"

    stack_manager._ensure_lambda_build = MagicMock()
    stack_manager._rebuild_lambda_packages()
    stack_manager._rebuild_lambda_packages()
    stack_manager._ensure_lambda_build.assert_called_once_with()


def test_build_kubectl_lambda_skips_incomplete_source(stack_manager: Any) -> None:
    with patch("cli.stacks._prepare_lambda_asset") as prepare:
        stack_manager._build_kubectl_lambda()
    prepare.assert_not_called()


def test_inference_streaming_builder_rejects_incomplete_and_bad_pin(
    stack_manager: Any,
    tmp_path: Path,
) -> None:
    import cli.stacks as stacks

    source, _build = stacks._INFERENCE_STREAMING_CDK_ASSET.paths(tmp_path)
    source.mkdir(parents=True)
    with pytest.raises(RuntimeError, match="package is incomplete"):
        stack_manager._build_inference_streaming_proxy_lambda()

    for name in stacks._INFERENCE_STREAMING_CDK_ASSET.source_inputs or ():
        (source / name).write_text("{}", encoding="utf-8")
    (source / "package.json").write_text(
        json.dumps({"packageManager": "npm@latest"}), encoding="utf-8"
    )
    with pytest.raises(RuntimeError, match="must pin an exact npm version"):
        stack_manager._build_inference_streaming_proxy_lambda()


def test_python_path_includes_only_existing_optional_paths(stack_manager: Any) -> None:
    with (
        patch("site.getsitepackages", return_value=["/site"]),
        patch("site.getusersitepackages", return_value="/missing-user-site"),
        patch("os.path.isdir", return_value=False),
        patch("cli.stacks.Path.exists", return_value=False),
        patch.dict(os.environ, {"PYTHONPATH": "/existing"}, clear=False),
    ):
        assert stack_manager._get_python_path().split(os.pathsep) == ["/site", "/existing"]


def test_terminate_cdk_process_handles_already_exited_and_posix_failure(
    stack_manager: Any,
) -> None:
    process = MagicMock()
    process.poll.return_value = 0
    stack_manager._terminate_cdk_process(process)
    process.wait.assert_not_called()

    process = MagicMock(pid=123)
    process.poll.side_effect = [None, None]
    with (
        patch("cli.stacks.os.name", "posix"),
        patch("cli.stacks.os.killpg", side_effect=[OSError("term"), None]) as killpg,
    ):
        stack_manager._terminate_cdk_process(process)
    assert killpg.call_args_list[0] == call(123, __import__("signal").SIGTERM)
    assert killpg.call_args_list[1] == call(123, __import__("signal").SIGKILL)
    process.wait.assert_called_once_with()


def test_run_cdk_cancellation_and_timeout_preserve_output(stack_manager: Any) -> None:
    stack_manager._cdk_path = "cdk"
    stack_manager._ensure_cdk_toolchain = MagicMock()
    stack_manager._get_python_path = MagicMock(return_value="pythonpath")
    stack_manager._cdk_cancel_event.set()
    with pytest.raises(RuntimeError, match="cancelled before process start"):
        stack_manager._run_cdk(["bootstrap"])

    stack_manager._cdk_cancel_event.clear()
    process = MagicMock(pid=99)
    process.communicate.side_effect = subprocess.TimeoutExpired(
        "cdk", 1, output="stdout", stderr="stderr"
    )
    with (
        patch("cli.stacks.subprocess.Popen", return_value=process),
        patch.object(stack_manager, "_terminate_cdk_process") as terminate,
        pytest.raises(subprocess.TimeoutExpired) as raised,
    ):
        stack_manager._run_cdk(["bootstrap"], capture_output=True, timeout=1)
    terminate.assert_called_once_with(process)
    assert raised.value.output == "stdout"
    assert raised.value.stderr == "stderr"


def test_synth_and_diff_without_stack_names_build_expected_commands(
    stack_manager: Any,
) -> None:
    stack_manager._ensure_lambda_build = MagicMock()
    stack_manager._run_cdk = MagicMock(
        side_effect=[
            SimpleNamespace(returncode=0, stdout="template", stderr=""),
            SimpleNamespace(returncode=1, stdout="", stderr="difference"),
        ]
    )
    assert stack_manager.synth(stack_name=None, quiet=False) == "template"
    assert stack_manager.diff(stack_name=None) == "difference"
    assert stack_manager._run_cdk.call_args_list[0] == call(["synth"], capture_output=True)
    assert stack_manager._run_cdk.call_args_list[1] == call(
        ["diff", "--no-color"], capture_output=True
    )


# ---------------------------------------------------------------------------
# Remaining CLI branches and CDK stack behavior
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("args", "patch_target"),
    [
        (
            ["regions", "remove", "us-west-2", "-y"],
            "cli.managed_config.remove_deployment_region",
        ),
        (
            ["bedrock", "set-mission-model", "model", "-y"],
            "cli.managed_config.set_mission_default_model",
        ),
        (
            ["bedrock", "set-capacity-advisor-model", "model", "-y"],
            "cli.managed_config.set_capacity_advisor_default_model",
        ),
        (
            ["bedrock", "set-claude-code-model", "model", "-y"],
            "cli.managed_config.set_claude_code_default_model",
        ),
        (
            ["bedrock", "set-codex-model", "model", "-y"],
            "cli.managed_config.set_codex_default_model",
        ),
        (
            ["bedrock", "set-codex-reasoning-effort", "high", "-y"],
            "cli.managed_config.set_codex_reasoning_effort",
        ),
    ],
)
def test_managed_stack_commands_report_changed(args: list[str], patch_target: str) -> None:
    with patch(patch_target, return_value=_report(True)):
        result = _invoke_stacks(args)
    assert result.exit_code == 0, result.output
    assert "managed summary" in result.output


def test_addons_install_propagates_unexpected_fence_error_to_read_failure() -> None:
    from cli.commands import stacks_cmd

    ssm = MagicMock()
    ssm.get_parameter.side_effect = _client_error("AccessDeniedException")
    formatter = MagicMock()
    with patch("boto3.client", return_value=ssm):
        assert stacks_cmd._addons_install_one(formatter, "gco", "us-east-1") is False
    assert "Could not read" in formatter.print_error.call_args.args[0]


def test_addons_install_reports_start_execution_failure() -> None:
    from cli.commands import stacks_cmd

    ssm = MagicMock()
    ssm.get_parameter.side_effect = [
        _client_error("ParameterNotFound"),
        {"Parameter": {"Value": '{"input":true}'}},
    ]
    sfn = MagicMock()
    sfn.list_state_machines.return_value = {
        "stateMachines": [{"name": "HelmInstall", "stateMachineArn": "arn:machine"}]
    }
    sfn.start_execution.side_effect = RuntimeError("start denied")
    formatter = MagicMock()
    with patch("boto3.client", side_effect=[ssm, sfn]):
        assert stacks_cmd._addons_install_one(formatter, "gco", "us-east-1") is False
    assert "Failed to start add-on install: start denied" in formatter.print_error.call_args.args[0]


def test_mcp_addons_status_without_selector_uses_default_cli_behavior() -> None:
    with _isolated_mcp_stacks() as loaded:
        assert asyncio.run(loaded.module.addons_status()) == '{"cli":"ok"}'
    loaded.run_cli.assert_called_once_with("stacks", "addons", "status")


def test_regional_stack_pure_validation_guards() -> None:
    from gco.stacks import regional_stack as regional

    with pytest.raises(ValueError, match="must be true or false"):
        regional._explicit_context_bool(object(), key="flag")
    with pytest.raises(ValueError, match="must not be negative"):
        regional._validated_resource_quota({"max_cpu": "-1"})

    with (
        patch.object(regional.yaml, "safe_load", return_value=[]),
        pytest.raises(RuntimeError, match="must be an object"),
    ):
        regional._load_helm_chart_order()
    with (
        patch.object(regional.yaml, "safe_load", return_value={"charts": {}}),
        pytest.raises(RuntimeError, match="non-empty charts object"),
    ):
        regional._load_helm_chart_order()
    with (
        patch.object(regional.yaml, "safe_load", return_value={"charts": {"": {}}}),
        pytest.raises(RuntimeError, match="invalid chart name"),
    ):
        regional._load_helm_chart_order()


def test_regional_stack_optional_feature_off_synthesis() -> None:
    """The real minimal stack omits optional images, services, and GA wiring."""
    from tests.test_regional_stack_feature_gap_coverage import _synthesize

    stack, template = _synthesize(
        feature_rich=False,
        global_accelerator=False,
        logical_name="coverage-100-regional-minimal",
    )
    assert stack.global_accelerator_enabled is False
    assert stack.fsx_file_system is None
    assert getattr(stack, "aurora_cluster", None) is None
    assert not template.find_resources("AWS::ElastiCache::ServerlessCache")


def test_regional_stack_manifest_policy_rejects_non_boolean() -> None:
    from tests import test_regional_stack_feature_gap_coverage as helper

    context = helper._app_context(feature_rich=False)
    context["job_validation_policy"]["require_accelerator_toleration"] = "yes"
    with (
        patch.object(helper, "_app_context", return_value=context),
        pytest.raises(ValueError, match="policy values must be booleans"),
    ):
        helper._synthesize(
            feature_rich=False,
            global_accelerator=False,
            logical_name="coverage-100-regional-invalid-policy",
        )


def test_regional_stack_unsupported_az_errors(monkeypatch: Any) -> None:
    from gco.stacks import regional_stack as regional

    subject = SimpleNamespace(deployment_region="us-east-1")
    monkeypatch.setenv("CDK_DEFAULT_ACCOUNT", "123456789012")
    with (
        patch.object(regional, "EKS_UNSUPPORTED_AZ_IDS", {"us-east-1": ("use1-az3",)}),
        patch("boto3.client", side_effect=RuntimeError("ec2 unavailable")),
        pytest.raises(RuntimeError, match="Unable to resolve"),
    ):
        regional.GCORegionalStack._resolve_unsupported_az_names(subject)

    client = MagicMock()
    client.describe_availability_zones.return_value = {"AvailabilityZones": "bad"}
    with (
        patch.object(regional, "EKS_UNSUPPORTED_AZ_IDS", {"us-east-1": ("use1-az3",)}),
        patch("boto3.client", return_value=client),
        pytest.raises(RuntimeError, match="malformed Availability Zone data"),
    ):
        regional.GCORegionalStack._resolve_unsupported_az_names(subject)

    client.describe_availability_zones.return_value = {"AvailabilityZones": []}
    with (
        patch.object(regional, "EKS_UNSUPPORTED_AZ_IDS", {"us-east-1": ("use1-az3",)}),
        patch("boto3.client", return_value=client),
        pytest.raises(RuntimeError, match="did not resolve"),
    ):
        regional.GCORegionalStack._resolve_unsupported_az_names(subject)


def test_regional_stack_configuration_shape_guards() -> None:
    from gco.stacks import regional_stack as regional

    node = MagicMock()
    node.try_get_context.return_value = ["invalid"]
    subject = SimpleNamespace(node=node, config=MagicMock())
    with pytest.raises(ValueError, match="volcano_image_mirror must be a mapping"):
        regional.GCORegionalStack._get_volcano_image_mirror_config(subject)

    node.try_get_context.return_value = {}
    subject.config.get_cluster_observability_enabled.return_value = False
    subject._cost_monitoring_active = MagicMock(return_value=False)
    subject._mlflow_active = MagicMock(return_value=False)
    with (
        patch.object(regional, "_HELM_CHART_CONFIG_KEYS", frozenset({"drifted"})),
        pytest.raises(RuntimeError, match="chart_map keys drifted"),
    ):
        regional.GCORegionalStack._get_enabled_helm_charts(subject)

    with pytest.raises(RuntimeError, match="must be the first Helm chart"):
        regional.GCORegionalStack._create_helm_teardown(subject, [])
    with pytest.raises(RuntimeError, match="must be the first Helm chart"):
        regional.GCORegionalStack._create_helm_teardown(subject, ["keda"])


def test_regional_stack_efs_guard_rejects_wrong_default_child() -> None:
    from gco.stacks import regional_stack as regional

    subject = SimpleNamespace(
        config=SimpleNamespace(get_project_name=lambda: "gco"),
        deployment_region="us-east-1",
        vpc=MagicMock(),
        cluster=SimpleNamespace(cluster_security_group=MagicMock()),
        disable_efs_automatic_backups=True,
    )
    fs = MagicMock()
    fs.node.default_child = object()
    with (
        patch.object(regional.ec2, "SecurityGroup", return_value=MagicMock()),
        patch.object(regional.efs, "FileSystem", return_value=fs),
        pytest.raises(TypeError, match="default child must be AWS::EFS::FileSystem"),
    ):
        regional.GCORegionalStack._create_efs(subject)


def test_global_stack_guards_and_replication_noops() -> None:
    from gco.stacks import global_stack as global_mod

    subject = SimpleNamespace(listener=None, accelerator=None)
    with pytest.raises(RuntimeError, match="requires the Global Accelerator listener"):
        global_mod.GCOGlobalStack._create_traffic_dial_controller(subject, {})
    with pytest.raises(RuntimeError, match="outputs require"):
        global_mod.GCOGlobalStack._create_outputs(subject)
    with pytest.raises(RuntimeError, match="endpoint groups are unavailable"):
        global_mod.GCOGlobalStack._create_endpoint_group(subject, "us-east-1")
    with pytest.raises(RuntimeError, match="unavailable"):
        global_mod.GCOGlobalStack.get_accelerator_arn(subject)
    with pytest.raises(RuntimeError, match="unavailable"):
        global_mod.GCOGlobalStack.get_listener_arn(subject)

    config = SimpleNamespace(get_regions=lambda: ["us-east-1", "us-west-2"])
    subject = SimpleNamespace(config=config, region="us-east-1")
    assert global_mod.GCOGlobalStack._resolve_replication_destinations(
        subject, "all_deployed_regions"
    ) == ["us-west-2"]
    assert global_mod.GCOGlobalStack._resolve_replication_destinations(
        subject, ["us-east-1", "eu-west-1"]
    ) == ["eu-west-1"]

    subject.images_config = {"replication": {"enabled": False, "destinations": []}}
    subject._resolve_replication_destinations = lambda destinations: (
        global_mod.GCOGlobalStack._resolve_replication_destinations(subject, destinations)
    )
    assert global_mod.GCOGlobalStack._create_image_replication_rule(subject) is None
    subject.images_config = {"replication": {"enabled": True, "destinations": ["us-east-1"]}}
    assert global_mod.GCOGlobalStack._create_image_replication_rule(subject) is None


def test_api_gateway_constructor_wires_initial_analytics_configuration() -> None:
    import aws_cdk as cdk

    from gco.stacks import api_gateway_global_stack as api_mod

    analytics = MagicMock()
    methods = {
        "_create_backend_tls": None,
        "_create_secret": MagicMock(),
        "_create_proxy_lambda": MagicMock(),
        "_create_inference_proxy_lambda": MagicMock(),
        "_create_aggregator_lambda": MagicMock(),
        "_create_api_gateway": MagicMock(),
        "_create_waf": None,
        "_create_outputs": None,
        "_wire_studio_routes": None,
        "_apply_nag_suppressions": None,
    }
    patches = [patch.object(api_mod.GCOApiGatewayGlobalStack, name) for name in methods]
    started = [item.start() for item in patches]
    mocks = dict(zip(methods, started, strict=True))
    try:
        stack = api_mod.GCOApiGatewayGlobalStack(
            cdk.App(),
            "Coverage100ApiAnalytics",
            global_accelerator_dns=None,
            analytics_config=analytics,
            env=cdk.Environment(account="123456789012", region="us-east-2"),
        )
    finally:
        for item in reversed(patches):
            item.stop()
    assert stack.analytics_config is analytics
    mocks["_wire_studio_routes"].assert_called_once_with()


def test_api_gateway_proxy_guards_and_no_ga_routes() -> None:
    import aws_cdk as cdk

    from gco.stacks import api_gateway_global_stack as api_mod

    subject = SimpleNamespace(ga_dns=None)
    with pytest.raises(RuntimeError, match="requires a Global Accelerator endpoint"):
        api_mod.GCOApiGatewayGlobalStack._create_proxy_lambda(subject)
    with pytest.raises(RuntimeError, match="requires Global Accelerator"):
        api_mod.GCOApiGatewayGlobalStack._create_inference_proxy_lambda(subject)

    app = cdk.App()
    blank = cdk.Stack(
        app,
        "Coverage100ApiInvalid",
        env=cdk.Environment(account="123456789012", region="us-east-2"),
    )
    blank.project_name = "gco"  # type: ignore[attr-defined]
    blank.api_gateway_config = {  # type: ignore[attr-defined]
        "log_level": "verbose",
        "throttle_rate_limit": 1,
        "throttle_burst_limit": 2,
        "metrics_enabled": True,
        "tracing_enabled": True,
    }
    with pytest.raises(ValueError, match="log_level must be one of"):
        api_mod.GCOApiGatewayGlobalStack._create_api_gateway(blank)  # type: ignore[arg-type]

    blank = cdk.Stack(
        app,
        "Coverage100ApiNoGa",
        env=cdk.Environment(account="123456789012", region="us-east-2"),
    )
    blank.project_name = "gco"  # type: ignore[attr-defined]
    blank.ga_dns = None  # type: ignore[attr-defined]
    blank.proxy_lambda = None  # type: ignore[attr-defined]
    blank.inference_proxy_lambda = None  # type: ignore[attr-defined]
    blank.api_gateway_config = {  # type: ignore[attr-defined]
        "log_level": "OFF",
        "throttle_rate_limit": 1,
        "throttle_burst_limit": 2,
        "metrics_enabled": False,
        "tracing_enabled": False,
    }
    blank._create_global_routes = MagicMock()  # type: ignore[attr-defined]
    api = api_mod.GCOApiGatewayGlobalStack._create_api_gateway(blank)  # type: ignore[arg-type]
    assert api is not None
    blank._create_global_routes.assert_called_once()  # type: ignore[attr-defined]


def test_regional_api_gateway_rejects_invalid_log_level() -> None:
    import aws_cdk as cdk

    from gco.stacks import regional_api_gateway_stack as regional_api

    app = cdk.App()
    blank = cdk.Stack(
        app,
        "Coverage100RegionalApiInvalid",
        env=cdk.Environment(account="123456789012", region="us-east-1"),
    )
    config = MagicMock()
    config.get_project_name.return_value = "gco"
    config.get_api_gateway_config.return_value = {"log_level": "debug"}
    blank.config = config  # type: ignore[attr-defined]
    blank.deployment_region = "us-east-1"  # type: ignore[attr-defined]
    with pytest.raises(ValueError, match="log_level must be one of"):
        regional_api.GCORegionalApiGatewayStack._create_api_gateway(blank)  # type: ignore[arg-type]


def test_analytics_cognito_domain_override_is_used_verbatim() -> None:
    import aws_cdk as cdk

    from tests.test_analytics_stack import _AnalyticsMockConfig, _synth_analytics

    class Config(_AnalyticsMockConfig):
        def get_analytics_config(self) -> dict[str, Any]:
            value = super().get_analytics_config()
            value["cognito"]["domain_prefix"] = "memorable-studio"
            return value

    template = _synth_analytics(
        app=cdk.App(),
        construct_id="coverage-100-analytics-domain",
        config=Config(),
    )
    domains = template.find_resources("AWS::Cognito::UserPoolDomain")
    assert len(domains) == 1
    assert next(iter(domains.values()))["Properties"]["Domain"] == "memorable-studio"


def test_monitoring_stack_without_ga_or_api_dependencies() -> None:
    import aws_cdk as cdk
    from aws_cdk import assertions

    from gco.stacks.monitoring_stack import GCOMonitoringStack
    from tests.test_monitoring_stack import (
        MockConfigLoader,
        create_mock_global_stack,
        create_mock_regional_stack,
    )

    global_stack = create_mock_global_stack()
    global_stack.accelerator_id = None
    stack = GCOMonitoringStack(
        cdk.App(),
        "Coverage100MonitoringNoApi",
        config=MockConfigLoader(cost_monitoring_enabled=False),
        global_stack=global_stack,
        regional_stacks=[
            create_mock_regional_stack("us-east-1"),
            create_mock_regional_stack("us-west-2"),
        ],
        api_gateway_stack=None,
        env=cdk.Environment(account="123456789012", region="us-east-2"),
    )
    template = assertions.Template.from_stack(stack)
    dashboards = template.find_resources("AWS::CloudWatch::Dashboard")
    body = json.dumps(next(iter(dashboards.values())))
    assert "API Gateway stack not configured" in body
    assert "Global Accelerator - New Flows" not in body


def test_monitoring_stack_api_without_proxy_still_monitors_rotation() -> None:
    import aws_cdk as cdk
    from aws_cdk import assertions

    from gco.stacks.monitoring_stack import GCOMonitoringStack
    from tests.test_monitoring_stack import (
        MockConfigLoader,
        create_mock_api_gateway_stack,
        create_mock_global_stack,
        create_mock_regional_stack,
    )

    api_stack = create_mock_api_gateway_stack()
    api_stack.proxy_lambda = None
    stack = GCOMonitoringStack(
        cdk.App(),
        "Coverage100MonitoringNoProxy",
        config=MockConfigLoader(cost_monitoring_enabled=False),
        global_stack=create_mock_global_stack(),
        regional_stacks=[create_mock_regional_stack("us-east-1")],
        api_gateway_stack=api_stack,
        env=cdk.Environment(account="123456789012", region="us-east-2"),
    )
    template = assertions.Template.from_stack(stack)
    alarms = template.find_resources("AWS::CloudWatch::Alarm")
    assert any(logical_id.startswith("RotationLambdaErrorsAlarm") for logical_id in alarms)
    assert not any(logical_id.startswith("ProxyLambdaErrorsAlarm") for logical_id in alarms)


# ---------------------------------------------------------------------------
# Resumed STACK checkpoint: CLI shape safety and asset/process lifecycle
# ---------------------------------------------------------------------------


def test_fsx_region_without_node_group_keeps_inherited_node_group() -> None:
    from gco.config.config_loader import ConfigLoader

    inherited = ConfigLoader(
        _ContextApp(
            {
                "fsx_lustre": {"node_group": {"min_size": 2}},
                "fsx_lustre_regions": {"us-east-1": {"enabled": True}},
            }
        )
    ).get_fsx_lustre_config("us-east-1")
    absent = ConfigLoader(
        _ContextApp({"fsx_lustre": {"node_group": {"min_size": 3}}})
    ).get_fsx_lustre_config("us-east-1")

    assert inherited["node_group"]["min_size"] == 2
    assert inherited["enabled"] is True
    assert absent["node_group"]["min_size"] == 3


def test_project_name_defaults_for_valid_non_mapping_json(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    from cli.commands import stacks_cmd

    monkeypatch.chdir(tmp_path)
    for document in ([], {"context": []}, {"context": "invalid"}, None):
        (tmp_path / "cdk.json").write_text(json.dumps(document), encoding="utf-8")
        assert stacks_cmd._project_name() == "gco"


def test_addons_status_treats_valid_non_mapping_json_as_unknown() -> None:
    from cli.commands import stacks_cmd

    paginator = MagicMock()
    paginator.paginate.return_value = [
        {
            "Parameters": [
                {"Name": "/gco/addons/us-east-1/list", "Value": "[]"},
                {"Name": "/gco/addons/us-east-1/null", "Value": "null"},
                {
                    "Name": "/gco/addons/us-east-1/scalars",
                    "Value": json.dumps({"status": 7, "message": ["detail"]}),
                },
            ]
        }
    ]
    client = MagicMock()
    client.get_paginator.return_value = paginator
    formatter = MagicMock()

    with patch("boto3.client", return_value=client):
        stacks_cmd._addons_status_one(formatter, "gco", "us-east-1")

    assert formatter.print_error.call_count == 3
    rendered = "\n".join(item.args[0] for item in formatter.print_error.call_args_list)
    assert "unknown" in rendered
    assert "['detail']" in rendered


def test_decode_addon_replay_input_preserves_legacy_raw_json() -> None:
    from cli.commands.stacks_cmd import _decode_addon_replay_input

    raw = '  {"legacy": true}'
    assert _decode_addon_replay_input(raw) == raw


@pytest.mark.parametrize(
    ("args", "patch_target"),
    [
        (
            ["regions", "remove", "us-west-2"],
            "cli.managed_config.remove_deployment_region",
        ),
        (
            ["bedrock", "set-mission-model", "model"],
            "cli.managed_config.set_mission_default_model",
        ),
        (
            ["bedrock", "set-capacity-advisor-model", "model"],
            "cli.managed_config.set_capacity_advisor_default_model",
        ),
        (
            ["bedrock", "set-claude-code-model", "model"],
            "cli.managed_config.set_claude_code_default_model",
        ),
        (
            ["bedrock", "set-codex-model", "model"],
            "cli.managed_config.set_codex_default_model",
        ),
        (
            ["bedrock", "set-codex-reasoning-effort", "high"],
            "cli.managed_config.set_codex_reasoning_effort",
        ),
    ],
)
def test_managed_stack_commands_confirm_interactive_changes(
    args: list[str],
    patch_target: str,
) -> None:
    with patch(patch_target, return_value=_report(True)):
        result = _invoke_stacks(args, input_text="y\n")
    assert result.exit_code == 0, result.output
    assert "managed summary" in result.output


def test_posix_lock_propagates_error_after_initial_contention() -> None:
    import cli.stacks as stacks

    with (
        patch(
            "fcntl.flock",
            side_effect=[
                BlockingIOError(errno.EAGAIN, "busy"),
                OSError(errno.EBADF, "lock disappeared"),
            ],
        ),
        patch.object(stacks.time, "monotonic", return_value=0.0),
        pytest.raises(OSError, match="lock disappeared"),
    ):
        stacks._acquire_posix_flock(
            1,
            lock_name="lock",
            exclusive=True,
            purpose="asset",
        )


def test_windows_file_lock_retries_contention_then_acquires() -> None:
    import cli.stacks as stacks

    handle = MagicMock()
    handle.fileno.return_value = 8
    msvcrt = SimpleNamespace(
        LK_NBLCK=1,
        LK_UNLCK=2,
        locking=MagicMock(side_effect=[BlockingIOError(errno.EAGAIN, "busy"), None]),
    )
    with (
        patch.object(stacks.os, "name", "nt"),
        patch.dict(sys.modules, {"msvcrt": msvcrt}),
        patch.object(stacks, "_file_lock_timeout_seconds", return_value=2.0),
        patch.object(stacks.time, "monotonic", return_value=0.0),
    ):
        stacks._acquire_file_lock(handle, exclusive=False, purpose="asset")

    assert msvcrt.locking.call_count == 2
    handle.seek.assert_called_with(0)


def test_publish_staged_asset_without_previous_tree(tmp_path: Path) -> None:
    import cli.stacks as stacks

    build = tmp_path / "asset"
    staging = tmp_path / ".asset.staging-new"
    staging.mkdir()
    (staging / "payload").write_text("new", encoding="utf-8")

    stacks._publish_staged_asset(staging, build)

    assert (build / "payload").read_text(encoding="utf-8") == "new"
    assert not staging.exists()


def test_prepare_lambda_asset_accepts_concurrent_fresh_publish(tmp_path: Path) -> None:
    import cli.stacks as stacks

    source = tmp_path / "lambda" / "source"
    build = tmp_path / "lambda" / "source-build"
    source.mkdir(parents=True)
    (source / "handler.py").write_text("source", encoding="utf-8")

    with (
        patch.object(stacks, "_asset_build_is_fresh", return_value=False),
        patch.object(stacks, "_recover_interrupted_asset_publish"),
        patch.object(stacks, "_asset_tree_digest", return_value="digest"),
        patch.object(stacks, "_asset_build_is_fresh_unlocked", return_value=True),
    ):
        changed = stacks._prepare_lambda_asset(
            source,
            build,
            source_inputs=None,
            display_name="demo",
            builder=MagicMock(),
        )

    assert changed is False


def test_prepare_lambda_asset_removes_failed_verification_staging(tmp_path: Path) -> None:
    import cli.stacks as stacks

    source = tmp_path / "lambda" / "source"
    build = tmp_path / "lambda" / "source-build"
    source.mkdir(parents=True)
    (source / "handler.py").write_text("source", encoding="utf-8")

    with (
        patch.object(stacks, "_asset_build_is_fresh", return_value=False),
        patch.object(stacks, "_asset_tree_digest", return_value="digest"),
        patch.object(
            stacks,
            "_asset_build_is_fresh_unlocked",
            side_effect=[False, False],
        ),
        patch.object(stacks, "_write_build_manifest"),
        pytest.raises(RuntimeError, match="completion manifest verification failed"),
    ):
        stacks._prepare_lambda_asset(
            source,
            build,
            source_inputs=None,
            display_name="demo",
            builder=lambda staging: (staging / "payload").write_text("built", encoding="utf-8"),
        )

    assert not list(build.parent.glob(".source-build.staging-*"))


def test_atomic_write_bytes_without_mode_preserves_content(tmp_path: Path) -> None:
    import cli.stacks as stacks

    target = tmp_path / "state.json"
    stacks._atomic_write_bytes(target, b'{"ok":true}')
    assert target.read_bytes() == b'{"ok":true}'


def test_windows_config_lock_wraps_acquisition_failure(tmp_path: Path) -> None:
    import cli.stacks as stacks

    handle = MagicMock()
    with (
        patch.object(stacks.os, "name", "nt"),
        patch.object(Path, "open", return_value=handle),
        patch.object(stacks, "_acquire_file_lock", side_effect=OSError("busy")),
        pytest.raises(stacks.ConfigMutationLockError, match="could not lock"),
        stacks._config_process_lock(tmp_path),
    ):
        pass
    handle.close.assert_called_once_with()


def test_cdk_asset_consumer_is_nested_and_cleans_thread_state(tmp_path: Path) -> None:
    import cli.stacks as stacks

    root_key = os.path.normcase(os.path.abspath(tmp_path))
    with (
        patch.object(stacks, "_CDK_ASSET_SPECS", ()),
        stacks.cdk_asset_consumer(tmp_path),
    ):
        assert stacks._thread_asset_consumers()[root_key] == 1
        with stacks.cdk_asset_consumer(tmp_path):
            assert stacks._thread_asset_consumers()[root_key] == 2
        assert stacks._thread_asset_consumers()[root_key] == 1
    assert root_key not in stacks._thread_asset_consumers()


def test_cdk_asset_consumer_fails_after_repeated_source_churn(tmp_path: Path) -> None:
    import cli.stacks as stacks

    source = tmp_path / "source"
    build = tmp_path / "build"
    source.mkdir()
    spec = SimpleNamespace(
        name="demo",
        source_inputs=None,
        paths=lambda _root: (source, build),
    )

    @contextmanager
    def shared_lock(*_args: Any, **_kwargs: Any) -> Iterator[None]:
        yield

    with (
        patch.object(stacks, "_CDK_ASSET_SPECS", (spec,)),
        patch.object(stacks, "_lambda_asset_lock", side_effect=shared_lock),
        patch.object(stacks, "_asset_build_is_fresh_unlocked", return_value=False),
        patch.object(stacks, "prepare_cdk_assets") as prepare,
        pytest.raises(RuntimeError, match="changed repeatedly"),
        stacks.cdk_asset_consumer(tmp_path),
    ):
        pass
    assert prepare.call_count == stacks._CDK_ASSET_CONSUMER_MAX_ATTEMPTS


def test_find_cdk_reports_missing_cli(stack_manager: Any) -> None:
    from cli.stacks import CdkToolchainError

    with (
        patch.object(Path, "is_file", return_value=False),
        patch("subprocess.run", side_effect=subprocess.CalledProcessError(1, "which")),
        patch("os.path.exists", return_value=False),
        pytest.raises(CdkToolchainError, match="CDK CLI is not installed"),
    ):
        stack_manager._find_cdk()


def test_diagnose_deploy_failure_reports_events_and_tolerates_status_error(
    stack_manager: Any,
    capsys: Any,
) -> None:
    stack_manager._get_deploy_region = MagicMock(return_value="us-east-1")
    client = MagicMock()
    client.describe_stack_events.return_value = {
        "StackEvents": [
            {
                "LogicalResourceId": "BrokenResource",
                "ResourceStatus": "CREATE_FAILED",
                "ResourceStatusReason": "invalid",
            }
        ]
    }
    client.describe_stacks.side_effect = RuntimeError("status unavailable")

    with patch("boto3.client", return_value=client):
        stack_manager._diagnose_deploy_failure("gco-global")

    output = capsys.readouterr().out
    assert "BrokenResource: CREATE_FAILED" in output
    assert "invalid" in output


def test_build_kubectl_lambda_packages_complete_source(
    stack_manager: Any,
    tmp_path: Path,
) -> None:
    import cli.stacks as stacks

    source, _build = stacks._KUBECTL_CDK_ASSET.paths(tmp_path)
    source.mkdir(parents=True)
    (source / "handler.py").write_text("handler", encoding="utf-8")
    (source / "requirements.txt").write_text("dependency==1", encoding="utf-8")
    manifests = source / "manifests"
    manifests.mkdir()
    (manifests / "one.yaml").write_text("kind: ConfigMap", encoding="utf-8")
    staging = tmp_path / "staging"
    staging.mkdir()

    def prepare(*_args: Any, builder: Any, **_kwargs: Any) -> bool:
        builder(staging)
        return True

    with (
        patch.object(stacks, "_prepare_lambda_asset", side_effect=prepare),
        patch.object(
            stacks.subprocess,
            "run",
            return_value=SimpleNamespace(returncode=0, stderr=""),
        ) as run,
    ):
        stack_manager._build_kubectl_lambda()

    assert (staging / "handler.py").read_text(encoding="utf-8") == "handler"
    assert (staging / "manifests" / "one.yaml").exists()
    run.assert_called_once()


def test_inference_streaming_builder_handles_pin_read_and_npm_failures(
    stack_manager: Any,
    tmp_path: Path,
) -> None:
    import cli.stacks as stacks

    source, _build = stacks._INFERENCE_STREAMING_CDK_ASSET.paths(tmp_path)
    source.mkdir(parents=True)
    for name in stacks._INFERENCE_STREAMING_CDK_ASSET.source_inputs or ():
        (source / name).write_text("{}", encoding="utf-8")
    (source / "package.json").write_text("not-json", encoding="utf-8")
    with pytest.raises(RuntimeError, match="Unable to read"):
        stack_manager._build_inference_streaming_proxy_lambda()

    (source / "package.json").write_text(
        json.dumps({"packageManager": "npm@10.9.2"}), encoding="utf-8"
    )
    staging = tmp_path / "node-staging"
    staging.mkdir()

    def run_builder(*_args: Any, builder: Any, **_kwargs: Any) -> bool:
        builder(staging)
        return True

    with (
        patch.object(stacks, "_prepare_lambda_asset", side_effect=run_builder),
        patch.object(stacks.shutil, "which", return_value=None),
        pytest.raises(RuntimeError, match="npm 10.9.2 is required"),
    ):
        stack_manager._build_inference_streaming_proxy_lambda()

    with (
        patch.object(stacks, "_prepare_lambda_asset", side_effect=run_builder),
        patch.object(stacks.shutil, "which", return_value="/usr/bin/npm"),
        patch.object(
            stacks.subprocess,
            "run",
            side_effect=[
                SimpleNamespace(returncode=0, stdout="10.9.2\n", stderr=""),
                SimpleNamespace(returncode=1, stdout="", stderr="install failed"),
            ],
        ),
        pytest.raises(RuntimeError, match="install pinned"),
    ):
        stack_manager._build_inference_streaming_proxy_lambda()


def test_python_path_includes_existing_user_and_repository_paths(
    stack_manager: Any,
) -> None:
    with (
        patch("site.getsitepackages", return_value=["/site"]),
        patch("site.getusersitepackages", return_value="/user-site"),
        patch("os.path.isdir", return_value=True),
        patch("cli.stacks.Path.exists", return_value=True),
        patch.dict(os.environ, {}, clear=True),
    ):
        paths = stack_manager._get_python_path().split(os.pathsep)
    assert paths[0:2] == ["/site", "/user-site"]
    assert str(PROJECT_ROOT) in paths


def test_terminate_cdk_process_windows_fallback_kills_process(
    stack_manager: Any,
) -> None:
    process = MagicMock(pid=321)
    process.poll.side_effect = [None, None]
    process.wait.side_effect = [subprocess.TimeoutExpired("wait", 30), None]
    with (
        patch("cli.stacks.os.name", "nt"),
        patch("cli.stacks.shutil.which", return_value=None),
    ):
        stack_manager._terminate_cdk_process(process)
    process.kill.assert_called_once_with()


def test_terminate_cdk_process_windows_ignores_taskkill_failure(
    stack_manager: Any,
) -> None:
    process = MagicMock(pid=654)
    process.poll.return_value = None
    process.wait.return_value = None
    with (
        patch("cli.stacks.os.name", "nt"),
        patch("cli.stacks.shutil.which", return_value="taskkill.exe"),
        patch(
            "cli.stacks.subprocess.run",
            side_effect=OSError("taskkill unavailable"),
        ),
    ):
        stack_manager._terminate_cdk_process(process)
    process.wait.assert_called_once_with(timeout=30)


def test_run_cdk_cancels_process_started_during_race(stack_manager: Any) -> None:
    process = MagicMock(pid=99)
    process.poll.return_value = None
    stack_manager._cdk_path = "cdk"
    stack_manager._ensure_cdk_toolchain = MagicMock()
    stack_manager._get_python_path = MagicMock(return_value="pythonpath")

    def start(*_args: Any, **_kwargs: Any) -> Any:
        stack_manager._cdk_cancel_event.set()
        return process

    with (
        patch("cli.stacks.subprocess.Popen", side_effect=start),
        patch.object(stack_manager, "_terminate_cdk_process") as terminate,
        pytest.raises(RuntimeError, match="cancelled during process start"),
    ):
        stack_manager._run_cdk(["bootstrap"])
    terminate.assert_called_once_with(process)


def test_run_cdk_terminates_process_on_caller_exception(stack_manager: Any) -> None:
    process = MagicMock(pid=77)
    process.communicate.side_effect = KeyboardInterrupt()
    stack_manager._cdk_path = "cdk"
    stack_manager._ensure_cdk_toolchain = MagicMock()
    stack_manager._get_python_path = MagicMock(return_value="pythonpath")
    with (
        patch("cli.stacks.subprocess.Popen", return_value=process),
        patch.object(stack_manager, "_terminate_cdk_process") as terminate,
        pytest.raises(KeyboardInterrupt),
    ):
        stack_manager._run_cdk(["bootstrap"])
    terminate.assert_called_once_with(process)
    assert stack_manager._active_cdk_processes == {}


def test_synth_and_diff_named_stack_include_optional_arguments(
    stack_manager: Any,
) -> None:
    stack_manager._ensure_lambda_build = MagicMock()
    stack_manager._run_cdk = MagicMock(
        side_effect=[
            SimpleNamespace(returncode=0, stdout="template", stderr=""),
            SimpleNamespace(returncode=0, stdout="difference", stderr=""),
        ]
    )

    assert stack_manager.synth("gco-global", quiet=True) == "template"
    assert stack_manager.diff("gco-global") == "difference"
    assert stack_manager._run_cdk.call_args_list == [
        call(["synth", "gco-global", "--quiet"], capture_output=True),
        call(["diff", "--no-color", "gco-global"], capture_output=True),
    ]


# ---------------------------------------------------------------------------
# Stack deployment, teardown, and orchestration state machines
# ---------------------------------------------------------------------------


def _ready_deploy_manager(manager: Any) -> None:
    manager._sync_lambda_sources = MagicMock()
    manager._ensure_lambda_build = MagicMock()
    manager._get_deploy_region = MagicMock(return_value="us-east-1")
    manager.ensure_bootstrapped = MagicMock(return_value=True)
    manager._validate_bootstrap_stack = MagicMock()
    manager._check_and_fix_stuck_stack = MagicMock()
    manager._mirror_images_if_enabled = MagicMock()
    manager._run_cdk = MagicMock(return_value=SimpleNamespace(returncode=0))
    manager._get_stack_status = MagicMock(return_value="CREATE_COMPLETE")
    manager._get_stack_last_update_time = MagicMock(return_value=None)
    manager._wait_for_stack_settle = MagicMock(return_value="CREATE_COMPLETE")
    manager._diagnose_deploy_failure = MagicMock()


@pytest.mark.parametrize(
    ("case", "message"),
    [
        ("unnamed", "exactly one named stack"),
        ("missing_callback", "both a run token"),
        ("bootstrap", "cannot auto-bootstrap"),
        ("authorizer", "exact stack authorizer"),
        ("target", "authoritative target state"),
        ("history", "prepared change-set history"),
    ],
)
def test_deploy_rejects_incomplete_strict_authority(
    stack_manager: Any,
    case: str,
    message: str,
) -> None:
    _ready_deploy_manager(stack_manager)
    callback = MagicMock()
    authorizer = MagicMock()
    kwargs: dict[str, Any] = {
        "stack_name": "gco-global",
        "strict_deployment_token": "run",
        "on_change_set_prepared": callback,
        "allow_bootstrap": False,
        "authorize_stack": authorizer,
        "expected_stack_ids": {"gco-global": None},
        "prepared_change_sets": {"gco-global": {}},
    }
    if case == "unnamed":
        kwargs["stack_name"] = None
    elif case == "missing_callback":
        kwargs["on_change_set_prepared"] = None
    elif case == "bootstrap":
        kwargs["allow_bootstrap"] = True
    elif case == "authorizer":
        kwargs["authorize_stack"] = None
    elif case == "target":
        kwargs["expected_stack_ids"] = None
    elif case == "history":
        kwargs["prepared_change_sets"] = None

    with pytest.raises(RuntimeError, match=message):
        stack_manager.deploy(**kwargs)


@pytest.mark.parametrize(
    ("case", "message"),
    [
        ("region", "Could not resolve deploy Region"),
        ("bootstrap", "could not be bootstrapped"),
        ("checkpoint", "checkpointed CDKToolkit"),
        ("runtime", "container runtime"),
    ],
)
def test_deploy_fails_closed_before_cdk_mutation(
    stack_manager: Any,
    case: str,
    message: str,
) -> None:
    import cli.stacks as stacks

    _ready_deploy_manager(stack_manager)
    kwargs: dict[str, Any] = {"stack_name": "gco-global"}
    runtime = "docker"
    if case == "region":
        stack_manager._get_deploy_region.return_value = None
    elif case == "bootstrap":
        stack_manager.ensure_bootstrapped.return_value = False
    elif case == "checkpoint":
        kwargs["allow_bootstrap"] = False
        kwargs["bootstrap_stacks"] = {}
    elif case == "runtime":
        runtime = None

    with (
        patch.object(stacks, "_detect_container_runtime", return_value=runtime),
        patch(
            "cli._container_runtime.container_runtime_error_message",
            return_value="container runtime unavailable",
        ),
        pytest.raises(RuntimeError, match=message),
    ):
        stack_manager.deploy(**kwargs)
    stack_manager._run_cdk.assert_not_called()


def test_deploy_builds_complete_cdk_command_and_all_stacks_variant(
    stack_manager: Any,
    monkeypatch: Any,
) -> None:
    import cli.stacks as stacks

    _ready_deploy_manager(stack_manager)
    monkeypatch.delenv("CDK_DOCKER", raising=False)
    with patch.object(stacks, "_detect_container_runtime", return_value="podman"):
        assert stack_manager.deploy(
            stack_name="gco-global",
            require_approval=False,
            outputs_file="outputs.json",
            parameters={"Size": "large"},
            tags={"Project": "gco"},
            progress="bar",
            output_dir="out",
            exclusively=True,
        )
    command = stack_manager._run_cdk.call_args.args[0]
    assert command == [
        "deploy",
        "gco-global",
        "--exclusively",
        "--require-approval",
        "never",
        "--outputs-file",
        "outputs.json",
        "--parameters",
        "Size=large",
        "--tags",
        "Project=gco",
        "--progress",
        "bar",
        "--output",
        "out",
    ]
    assert stack_manager._run_cdk.call_args.kwargs["env"] == {"CDK_DOCKER": "podman"}

    stack_manager._run_cdk.reset_mock()
    monkeypatch.setenv("CDK_DOCKER", "configured")
    with patch.object(stacks, "_detect_container_runtime", return_value="docker"):
        assert stack_manager.deploy(all_stacks=True)
    assert stack_manager._run_cdk.call_args.args[0] == [
        "deploy",
        "--all",
        "--progress",
        "events",
    ]
    assert stack_manager._run_cdk.call_args.kwargs["env"] is None


@pytest.mark.parametrize(
    ("run_result", "statuses", "settled", "fresh", "expected"),
    [
        (1, ["CREATE_IN_PROGRESS"], "UPDATE_COMPLETE", None, True),
        (1, ["UPDATE_COMPLETE"], None, "fresh", True),
        (1, ["CREATE_COMPLETE"], None, "stale", False),
        (0, ["UPDATE_ROLLBACK_COMPLETE"], None, None, False),
        (1, [None], None, None, False),
    ],
)
def test_deploy_reconciles_cdk_and_cloudformation_outcomes(
    stack_manager: Any,
    run_result: int,
    statuses: list[str | None],
    settled: str | None,
    fresh: str | None,
    expected: bool,
) -> None:
    import cli.stacks as stacks

    _ready_deploy_manager(stack_manager)
    stack_manager._run_cdk.return_value = SimpleNamespace(returncode=run_result)
    stack_manager._get_stack_status.side_effect = (
        [*statuses, "UPDATE_COMPLETE"] if expected else statuses
    )
    stack_manager._wait_for_stack_settle.return_value = settled
    if fresh == "fresh":
        from datetime import UTC, datetime

        stack_manager._get_stack_last_update_time.return_value = datetime.max.replace(tzinfo=UTC)
    elif fresh == "stale":
        from datetime import UTC, datetime

        stack_manager._get_stack_last_update_time.return_value = datetime.min.replace(tzinfo=UTC)

    with patch.object(stacks, "_detect_container_runtime", return_value="docker"):
        assert stack_manager.deploy(stack_name="gco-global") is expected
    if not expected:
        stack_manager._diagnose_deploy_failure.assert_called_once_with("gco-global")


def test_deploy_timeout_and_cancellation_are_reconciled_separately(
    stack_manager: Any,
) -> None:
    import cli.stacks as stacks

    _ready_deploy_manager(stack_manager)
    stack_manager._run_cdk.side_effect = subprocess.TimeoutExpired("cdk", 3)
    stack_manager._get_stack_status.return_value = None
    with patch.object(stacks, "_detect_container_runtime", return_value="docker"):
        assert stack_manager.deploy(stack_name="gco-global") is False

    _ready_deploy_manager(stack_manager)

    def cancel(*_args: Any, **_kwargs: Any) -> Any:
        stack_manager._cdk_cancel_event.set()
        return SimpleNamespace(returncode=0)

    stack_manager._run_cdk.side_effect = cancel
    with (
        patch.object(stacks, "_detect_container_runtime", return_value="docker"),
        pytest.raises(RuntimeError, match="cancelled before AWS-side reconciliation"),
    ):
        stack_manager.deploy(stack_name="gco-global")
    stack_manager._cdk_cancel_event.clear()


def _strict_deploy_kwargs() -> dict[str, Any]:
    return {
        "stack_name": "gco-analytics",
        "require_approval": False,
        "allow_bootstrap": False,
        "bootstrap_stacks": {"us-east-1": {"stack_id": "toolkit", "status": "ok"}},
        "expected_stack_ids": {"gco-analytics": None},
        "prepared_change_sets": {"gco-analytics": {}},
        "authorize_stack": MagicMock(),
        "strict_deployment_token": "run",
        "on_change_set_prepared": MagicMock(),
    }


def test_strict_deploy_diagnoses_execution_failure_and_exception(
    stack_manager: Any,
) -> None:
    import cli.stacks as stacks

    _ready_deploy_manager(stack_manager)
    stack_manager._describe_stack_target = MagicMock(return_value=None)
    stack_manager._preflight_strict_change_set = MagicMock()
    stack_manager._execute_prepared_change_set = MagicMock(return_value=False)
    with patch.object(stacks, "_detect_container_runtime", return_value="docker"):
        assert stack_manager.deploy(**_strict_deploy_kwargs()) is False
    stack_manager._diagnose_deploy_failure.assert_called_once_with("gco-analytics")

    stack_manager._diagnose_deploy_failure.reset_mock()
    stack_manager._execute_prepared_change_set.side_effect = RuntimeError("change set failed")
    with (
        patch.object(stacks, "_detect_container_runtime", return_value="docker"),
        pytest.raises(RuntimeError, match="change set failed"),
    ):
        stack_manager.deploy(**_strict_deploy_kwargs())
    stack_manager._diagnose_deploy_failure.assert_called_once_with("gco-analytics")


def test_strict_and_standard_analytics_deploy_refresh_routes(
    stack_manager: Any,
) -> None:
    import cli.stacks as stacks

    original = stacks.StackManager.deploy
    _ready_deploy_manager(stack_manager)
    stack_manager._describe_stack_target = MagicMock(return_value=None)
    stack_manager._preflight_strict_change_set = MagicMock()
    stack_manager._execute_prepared_change_set = MagicMock(return_value=True)
    with (
        patch.object(stacks, "_detect_container_runtime", return_value="docker"),
        patch.object(stack_manager, "deploy", return_value=True) as recursive,
    ):
        assert original(stack_manager, **_strict_deploy_kwargs()) is True
    assert recursive.call_args.kwargs["stack_name"] == "gco-api-gateway"
    assert recursive.call_args.kwargs["strict_deployment_token"].endswith("-analytics-routes")

    _ready_deploy_manager(stack_manager)
    with (
        patch.object(stacks, "_detect_container_runtime", return_value="docker"),
        patch.object(stack_manager, "deploy", return_value=True) as recursive,
    ):
        assert original(stack_manager, stack_name="gco-analytics") is True
    assert recursive.call_args.kwargs["stack_name"] == "gco-api-gateway"
    assert recursive.call_args.kwargs["exclusively"] is True


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"all_stacks": True, "expected_stack_id": "arn:x"}, "cannot use all_stacks"),
        ({"expected_stack_id": "arn:x"}, "exactly one named stack"),
        (
            {
                "stack_name": "gco-global",
                "expected_stack_ids": {"other": "arn:x"},
                "authorize_stack": MagicMock(),
            },
            "lacks authoritative target state",
        ),
        (
            {
                "stack_name": "gco-global",
                "expected_stack_id": "arn:a",
                "expected_stack_ids": {"gco-global": "arn:b"},
                "authorize_stack": MagicMock(),
            },
            "Conflicting expected stack identities",
        ),
        (
            {"stack_name": "gco-global", "expected_stack_id": "arn:x"},
            "exact stack authorizer",
        ),
    ],
)
def test_destroy_rejects_invalid_identity_fences(
    stack_manager: Any,
    kwargs: dict[str, Any],
    message: str,
) -> None:
    with pytest.raises(RuntimeError, match=message):
        stack_manager._destroy(**kwargs)


def test_destroy_honors_registry_and_strict_teardown_guards(
    stack_manager: Any,
) -> None:
    stack_manager._image_registry_destroy_preflight = MagicMock(return_value=False)
    assert stack_manager._destroy(stack_name="gco-global") is False

    stack_manager._image_registry_destroy_preflight.return_value = True
    stack_manager._cdk_cancel_event.set()
    with pytest.raises(RuntimeError, match="cancelled before deleting"):
        stack_manager._destroy(
            stack_name="gco-global",
            expected_stack_id="arn:stack",
            authorize_stack=MagicMock(),
        )
    stack_manager._cdk_cancel_event.clear()

    with pytest.raises(RuntimeError, match="complete expected stack identity map"):
        stack_manager._destroy(
            stack_name="gco-analytics",
            expected_stack_id="arn:stack",
            authorize_stack=MagicMock(),
        )

    stack_manager._remove_api_gateway_analytics_dependency = MagicMock(return_value=False)
    assert (
        stack_manager._destroy(
            stack_name="gco-analytics",
            expected_stack_ids={"gco-analytics": "arn:stack"},
            authorize_stack=MagicMock(),
        )
        is False
    )

    stack_manager._remove_api_gateway_analytics_dependency.return_value = True
    stack_manager._cloudformation_delete_stack = MagicMock(return_value=True)
    assert stack_manager._destroy(
        stack_name="gco-analytics",
        expected_stack_ids={"gco-analytics": "arn:stack"},
        authorize_stack=MagicMock(),
        strict_deployment_token="run",
    )
    stack_manager._cloudformation_delete_stack.assert_called_once()


def _ready_destroy_manager(manager: Any) -> None:
    manager._get_orphan_regional_api_region = MagicMock(return_value=None)
    manager._stack_exists_in_cloudformation = MagicMock(return_value=False)
    manager._ensure_analytics_enabled_for_destroy = MagicMock(return_value=False)
    manager._remove_api_gateway_analytics_dependency = MagicMock(return_value=True)
    manager._restore_analytics_disabled = MagicMock()
    manager._run_cdk = MagicMock(return_value=SimpleNamespace(returncode=0))
    manager._get_stack_status = MagicMock(return_value=None)
    manager._wait_for_stack_delete_convergence = MagicMock(return_value=True)
    manager._print_stack_delete_heartbeat = MagicMock()
    manager._cloudformation_delete_stack = MagicMock(return_value=True)
    manager._image_registry_destroy_preflight = MagicMock(return_value=True)


def test_destroy_handles_orphan_regional_api_absence_and_direct_delete(
    stack_manager: Any,
) -> None:
    _ready_destroy_manager(stack_manager)
    stack_manager._get_orphan_regional_api_region.return_value = "us-west-2"
    assert stack_manager._destroy(stack_name="gco-regional-api-us-west-2") is True
    stack_manager._run_cdk.assert_not_called()

    stack_manager._stack_exists_in_cloudformation.return_value = True
    assert stack_manager._destroy(stack_name="gco-regional-api-us-west-2") is True
    stack_manager._cloudformation_delete_stack.assert_called_once_with(
        "gco-regional-api-us-west-2",
        expected_stack_id=None,
        authorize_stack=None,
    )


def test_destroy_restores_analytics_toggle_when_dependency_cannot_drop(
    stack_manager: Any,
) -> None:
    _ready_destroy_manager(stack_manager)
    stack_manager._stack_exists_in_cloudformation.return_value = True
    stack_manager._ensure_analytics_enabled_for_destroy.return_value = True
    stack_manager._remove_api_gateway_analytics_dependency.return_value = False

    assert stack_manager._destroy(stack_name="gco-analytics") is False
    stack_manager._restore_analytics_disabled.assert_called_once_with()
    stack_manager._run_cdk.assert_not_called()


def test_destroy_builds_commands_and_reconciles_cloudformation(
    stack_manager: Any,
) -> None:
    _ready_destroy_manager(stack_manager)
    assert stack_manager._destroy(
        all_stacks=True,
        force=True,
        output_dir="out",
    )
    assert stack_manager._run_cdk.call_args.args[0] == [
        "destroy",
        "--all",
        "--force",
        "--output",
        "out",
    ]

    _ready_destroy_manager(stack_manager)
    stack_manager._stack_exists_in_cloudformation.return_value = False
    stack_manager._run_cdk.side_effect = subprocess.TimeoutExpired("cdk", 3)
    assert stack_manager._destroy(stack_name="gco-us-east-1") is True

    _ready_destroy_manager(stack_manager)
    stack_manager._stack_exists_in_cloudformation.return_value = True
    stack_manager._get_stack_status.return_value = "DELETE_IN_PROGRESS"
    assert stack_manager._destroy(stack_name="gco-us-east-1") is True
    stack_manager._wait_for_stack_delete_convergence.assert_called_once()

    _ready_destroy_manager(stack_manager)
    stack_manager._stack_exists_in_cloudformation.return_value = True
    stack_manager._get_stack_status.return_value = "DELETE_FAILED"
    assert stack_manager._destroy(stack_name="gco-us-east-1") is False
    stack_manager._print_stack_delete_heartbeat.assert_called_once()

    _ready_destroy_manager(stack_manager)
    stack_manager._stack_exists_in_cloudformation.return_value = True
    stack_manager._get_stack_status.return_value = "UPDATE_COMPLETE"
    assert stack_manager._destroy(stack_name="gco-us-east-1") is True
    stack_manager._cloudformation_delete_stack.assert_called_once_with("gco-us-east-1")

    _ready_destroy_manager(stack_manager)
    stack_manager._stack_exists_in_cloudformation.return_value = True
    stack_manager._run_cdk.return_value = SimpleNamespace(returncode=1)
    stack_manager._get_stack_status.return_value = None
    assert stack_manager._destroy(stack_name="gco-us-east-1") is False


def test_destroy_cancellation_after_cdk_does_not_reconcile(
    stack_manager: Any,
) -> None:
    _ready_destroy_manager(stack_manager)

    def cancel(*_args: Any, **_kwargs: Any) -> Any:
        stack_manager._cdk_cancel_event.set()
        return SimpleNamespace(returncode=0)

    stack_manager._run_cdk.side_effect = cancel
    with pytest.raises(RuntimeError, match="cancelled before AWS-side reconciliation"):
        stack_manager._destroy(stack_name="gco-us-east-1")
    stack_manager._cdk_cancel_event.clear()


def test_image_registry_inventory_aggregates_owned_repositories(
    stack_manager: Any,
) -> None:
    manager = MagicMock()
    manager.list_repos.return_value = [
        {"name": "gco/one"},
        {"name": "foreign/two"},
        {"name": "gco/broken"},
    ]
    manager.list_tags.side_effect = [
        [{"size_bytes": 10}, {"size_bytes": "unknown"}],
        RuntimeError("cannot list"),
    ]
    manager._collect_inference_image_refs.return_value = {"a", "b"}
    manager._collect_recent_job_image_refs.return_value = {"job"}

    with patch("cli.images.ImageManager", return_value=manager):
        inventory = stack_manager._build_image_registry_inventory()

    assert inventory == {
        "repo_count": 3,
        "tag_count": 2,
        "total_bytes": 10,
        "endpoint_refs": 2,
        "job_refs": 1,
    }


def test_describe_stack_target_treats_missing_exact_and_named_stack_as_absent(
    stack_manager: Any,
) -> None:
    stack_manager._get_destroy_region = MagicMock(return_value="us-east-1")
    client = MagicMock()
    client.describe_stacks.side_effect = _client_error(
        "ValidationError", "Stack does not exist", "DescribeStacks"
    )
    with patch("boto3.client", return_value=client):
        assert (
            stack_manager._describe_stack_target(
                "gco-global",
                expected_stack_id="arn:stack",
            )
            is None
        )
    assert client.describe_stacks.call_count == 2


def test_delete_convergence_stops_when_stack_leaves_delete_state(
    stack_manager: Any,
) -> None:
    stack_manager._stack_exists_in_cloudformation = MagicMock(return_value=True)
    stack_manager._get_stack_status = MagicMock(return_value="UPDATE_COMPLETE")
    stack_manager._print_stack_delete_heartbeat = MagicMock()
    with patch("cli.stacks.time.sleep"):
        assert (
            stack_manager._wait_for_stack_delete_convergence(
                "gco-global",
                timeout=1,
                poll_interval=0.01,
                heartbeat_interval=0.01,
            )
            is False
        )
    stack_manager._print_stack_delete_heartbeat.assert_called()


def test_regional_api_region_validation_and_authoritative_config(
    stack_manager: Any,
    tmp_path: Path,
) -> None:
    import cli.stacks as stacks

    stacks._known_cloudformation_regions.cache_clear()
    with patch.object(
        stacks,
        "_known_cloudformation_regions",
        return_value=frozenset({"us-east-1", "us-west-2"}),
    ):
        assert stack_manager._validated_regional_api_region("other") is None
        assert stack_manager._validated_regional_api_region("gco-regional-api-") is None
        assert (
            stack_manager._validated_regional_api_region("gco-regional-api-us-west-2")
            == "us-west-2"
        )
        assert stack_manager._validated_regional_api_region("gco-regional-api-nope") is None

        documents: list[object] = [
            [],
            {"context": []},
            {"context": {"project_name": "other"}},
            {"context": {"project_name": "gco", "deployment_regions": []}},
            {
                "context": {
                    "project_name": "gco",
                    "deployment_regions": {
                        "global": "invalid",
                        "api_gateway": "us-east-1",
                        "monitoring": "us-east-1",
                        "regional": ["us-east-1"],
                    },
                }
            },
        ]
        for document in documents:
            (tmp_path / "cdk.json").write_text(json.dumps(document), encoding="utf-8")
            assert stack_manager._configured_regional_api_regions() is None

        valid = {
            "context": {
                "project_name": "gco",
                "deployment_regions": {
                    "global": "us-east-1",
                    "api_gateway": "us-east-1",
                    "monitoring": "us-east-1",
                    "regional": ["us-east-1"],
                },
            }
        }
        (tmp_path / "cdk.json").write_text(json.dumps(valid), encoding="utf-8")
        with patch.object(stacks, "validated_deployment_partition", return_value="aws"):
            assert stack_manager._configured_regional_api_regions() == (
                frozenset({"us-east-1"}),
                "aws",
            )


def test_orphan_regional_api_requires_absence_and_matching_partition(
    stack_manager: Any,
) -> None:
    import cli.stacks as stacks

    stack_manager._validated_regional_api_region = MagicMock(return_value="us-west-2")
    stack_manager._configured_regional_api_regions = MagicMock(
        return_value=(frozenset({"us-east-1"}), "aws")
    )
    with patch.object(stacks, "validated_deployment_partition", return_value="aws"):
        assert (
            stack_manager._get_orphan_regional_api_region("gco-regional-api-us-west-2")
            == "us-west-2"
        )
    stack_manager._configured_regional_api_regions.return_value = (
        frozenset({"us-west-2"}),
        "aws",
    )
    assert stack_manager._get_orphan_regional_api_region("gco-regional-api-us-west-2") is None
    stack_manager._configured_regional_api_regions.return_value = (
        frozenset({"us-east-1"}),
        "aws",
    )
    with patch.object(stacks, "validated_deployment_partition", return_value="aws-us-gov"):
        assert stack_manager._get_orphan_regional_api_region("gco-regional-api-us-west-2") is None


def test_analytics_dependency_fast_paths_and_failed_redeploy(
    stack_manager: Any,
) -> None:
    stack_manager._stack_exists_in_cloudformation = MagicMock(return_value=False)
    stack_manager._api_gateway_imports_from_analytics = MagicMock(return_value=True)
    assert stack_manager._remove_api_gateway_analytics_dependency()

    stack_manager._stack_exists_in_cloudformation.return_value = True
    stack_manager._api_gateway_imports_from_analytics.return_value = False
    assert stack_manager._remove_api_gateway_analytics_dependency()

    stack_manager._api_gateway_imports_from_analytics.side_effect = [True, False]
    stack_manager.deploy = MagicMock(return_value=False)
    with (
        patch("cli.stacks.get_analytics_config", return_value={"enabled": True}),
        patch("cli.stacks.update_analytics_config") as update,
    ):
        assert stack_manager._remove_api_gateway_analytics_dependency()
    assert update.call_args_list == [call({"enabled": False}), call({"enabled": True})]

    stack_manager._api_gateway_imports_from_analytics.side_effect = [True, True]
    with (
        patch("cli.stacks.get_analytics_config", return_value={"enabled": False}),
        patch("cli.stacks.update_analytics_config"),
    ):
        assert stack_manager._remove_api_gateway_analytics_dependency() is False


def test_analytics_dependency_strict_mode_requires_change_set_authority(
    stack_manager: Any,
) -> None:
    stack_manager._stack_exists_in_cloudformation = MagicMock(return_value=True)
    stack_manager._api_gateway_imports_from_analytics = MagicMock(return_value=True)
    with pytest.raises(RuntimeError, match="prepared-change-set authority"):
        stack_manager._remove_api_gateway_analytics_dependency(
            expected_stack_ids={"gco-api-gateway": "arn:api"}
        )


def test_bootstrap_commands_cache_and_validation(stack_manager: Any) -> None:
    stack_manager._run_cdk = MagicMock(return_value=SimpleNamespace(returncode=0))
    assert stack_manager.bootstrap(account="123", region="us-east-1")
    assert stack_manager._run_cdk.call_args.args[0] == [
        "bootstrap",
        "aws://123/us-east-1",
    ]
    stack_manager.bootstrap(region="us-west-2")
    assert stack_manager._run_cdk.call_args.args[0] == [
        "bootstrap",
        "aws://unknown-account/us-west-2",
    ]

    client = MagicMock()
    client.describe_stacks.return_value = {"Stacks": [{"StackStatus": "CREATE_COMPLETE"}]}
    with patch("boto3.client", return_value=client):
        assert stack_manager.is_bootstrapped("us-east-1") is True
        assert stack_manager.is_bootstrapped("us-east-1") is True
    client.describe_stacks.assert_called_once()


def test_bootstrap_validation_rejects_changed_checkpoint(stack_manager: Any) -> None:
    healthy = {"stack_id": "arn:toolkit", "status": "CREATE_COMPLETE"}
    with pytest.raises(RuntimeError, match="Invalid checkpointed"):
        stack_manager._validate_bootstrap_stack("us-east-1", {})

    client = MagicMock()
    client.describe_stacks.side_effect = RuntimeError("offline")
    with (
        patch("boto3.client", return_value=client),
        pytest.raises(RuntimeError, match="Could not revalidate"),
    ):
        stack_manager._validate_bootstrap_stack("us-east-1", healthy)

    client.describe_stacks.side_effect = None
    client.describe_stacks.return_value = {"Stacks": []}
    with (
        patch("boto3.client", return_value=client),
        pytest.raises(RuntimeError, match="invalid identity"),
    ):
        stack_manager._validate_bootstrap_stack("us-east-1", healthy)

    client.describe_stacks.return_value = {
        "Stacks": [
            {
                "StackName": "CDKToolkit",
                "StackId": "arn:toolkit",
                "StackStatus": "UPDATE_COMPLETE",
            }
        ]
    }
    with (
        patch("boto3.client", return_value=client),
        pytest.raises(RuntimeError, match="status changed"),
    ):
        stack_manager._validate_bootstrap_stack("us-east-1", healthy)


def test_ensure_bootstrapped_short_circuits_and_updates_cache(
    stack_manager: Any,
) -> None:
    stack_manager.is_bootstrapped = MagicMock(side_effect=[True, False])
    stack_manager.bootstrap = MagicMock(return_value=True)
    assert stack_manager.ensure_bootstrapped("us-east-1") is True
    stack_manager.bootstrap.assert_not_called()
    assert stack_manager.ensure_bootstrapped("us-west-2") is True
    assert stack_manager._bootstrap_cache["us-west-2"] is True


def _strict_change_set_response(
    *,
    status: str = "CREATE_COMPLETE",
    execution_status: str = "AVAILABLE",
) -> dict[str, Any]:
    return {
        "ChangeSetId": ("arn:aws:cloudformation:us-east-1:123456789012:changeSet/change/11111111"),
        "ChangeSetName": "change",
        "StackId": ("arn:aws:cloudformation:us-east-1:123456789012:stack/gco-global/22222222"),
        "Status": status,
        "ExecutionStatus": execution_status,
        "Tags": [{"Key": "Project", "Value": "gco"}],
    }


def test_change_set_inspection_cancellation_fails_before_checkpoint(
    stack_manager: Any,
) -> None:
    stack_manager._get_deploy_region = MagicMock(return_value="us-east-1")
    stack_manager._cdk_cancel_event.set()
    client = MagicMock()
    client.describe_change_set.side_effect = _client_error("ChangeSetNotFound")
    with (
        patch("boto3.client", return_value=client),
        pytest.raises(RuntimeError, match="inspection cancelled"),
    ):
        stack_manager._execute_prepared_change_set(
            stack_name="gco-global",
            change_set_name="change",
            expected_stack_id=None,
            expected_tags=None,
            prepared_change_sets={},
            preparation_succeeded=False,
            authorize_stack=None,
            on_change_set_prepared=MagicMock(),
            allow_noop=False,
            timeout=1,
        )
    stack_manager._cdk_cancel_event.clear()


def test_change_set_execution_reports_unhealthy_settlement(
    stack_manager: Any,
) -> None:
    response = _strict_change_set_response()
    stack_id = response["StackId"]
    stack_manager._get_deploy_region = MagicMock(return_value="us-east-1")
    stack_manager._describe_stack_target = MagicMock(
        return_value=(
            "us-east-1",
            MagicMock(),
            {"StackStatus": "REVIEW_IN_PROGRESS"},
        )
    )
    stack_manager._wait_for_stack_settle = MagicMock(return_value="ROLLBACK_COMPLETE")
    client = MagicMock()
    client.describe_change_set.return_value = response
    callback = MagicMock()
    with patch("boto3.client", return_value=client):
        assert (
            stack_manager._execute_prepared_change_set(
                stack_name="gco-global",
                change_set_name="change",
                expected_stack_id=None,
                expected_tags={"Project": "gco"},
                prepared_change_sets={},
                preparation_succeeded=True,
                authorize_stack=None,
                on_change_set_prepared=callback,
                allow_noop=False,
                timeout=1,
            )
            is False
        )
    callback.assert_called_once()
    client.execute_change_set.assert_called_once_with(ChangeSetName=response["ChangeSetId"])
    stack_manager._wait_for_stack_settle.assert_called_once_with(
        "gco-global", timeout=1, stack_identifier=stack_id
    )


@pytest.mark.parametrize(
    ("case", "message"),
    [
        ("callback", "both a run token"),
        ("bootstrap", "cannot auto-bootstrap"),
        ("authorizer", "exact authorizer"),
        ("targets", "target identities"),
        ("history", "change-set history"),
        ("target_mismatch", "target map does not match"),
        ("history_mismatch", "history does not match"),
    ],
)
def test_deploy_orchestration_rejects_incomplete_strict_preflight(
    stack_manager: Any,
    case: str,
    message: str,
) -> None:
    stack_manager.list_stacks = MagicMock(return_value=["gco-global"])
    kwargs: dict[str, Any] = {
        "strict_deployment_token": "run",
        "on_change_set_prepared": MagicMock(),
        "allow_bootstrap": False,
        "authorize_stack": MagicMock(),
        "expected_stack_ids": {"gco-global": None},
        "prepared_change_sets": {"gco-global": {}},
    }
    if case == "callback":
        kwargs["on_change_set_prepared"] = None
    elif case == "bootstrap":
        kwargs["allow_bootstrap"] = True
    elif case == "authorizer":
        kwargs["authorize_stack"] = None
    elif case == "targets":
        kwargs["expected_stack_ids"] = None
    elif case == "history":
        kwargs["prepared_change_sets"] = None
    elif case == "target_mismatch":
        kwargs["expected_stack_ids"] = {"other": None}
    elif case == "history_mismatch":
        kwargs["prepared_change_sets"] = {"other": {}}
    with pytest.raises(RuntimeError, match=message):
        stack_manager.deploy_orchestrated(**kwargs)


def test_deploy_orchestration_runs_all_sequential_phases_and_callbacks(
    stack_manager: Any,
) -> None:
    names = [
        "gco-global",
        "gco-api-gateway",
        "gco-us-east-1",
        "gco-regional-api-us-east-1",
        "gco-monitoring",
    ]
    stack_manager.list_stacks = MagicMock(return_value=names)
    stack_manager.deploy = MagicMock(return_value=True)
    started = MagicMock()
    completed = MagicMock()

    result = stack_manager.deploy_orchestrated(
        on_stack_start=started,
        on_stack_complete=completed,
    )

    assert result == (True, names, [])
    assert [item.args[0] for item in started.call_args_list] == names
    assert [item.args for item in completed.call_args_list] == [(name, True) for name in names]
    regional_call = next(
        item
        for item in stack_manager.deploy.call_args_list
        if item.kwargs["stack_name"] == "gco-us-east-1"
    )
    assert regional_call.kwargs["exclusively"] is True


def test_deploy_orchestration_parallel_phase_failure_stops_dependents(
    stack_manager: Any,
) -> None:
    names = [
        "gco-global",
        "gco-us-east-1",
        "gco-us-west-2",
        "gco-regional-api-us-east-1",
        "gco-regional-api-us-west-2",
        "gco-monitoring",
    ]
    stack_manager.list_stacks = MagicMock(return_value=names)
    stack_manager.deploy = MagicMock(return_value=True)
    stack_manager._deploy_stacks_parallel = MagicMock(
        side_effect=[
            (["gco-us-east-1", "gco-us-west-2"], []),
            (["gco-regional-api-us-east-1"], ["gco-regional-api-us-west-2"]),
        ]
    )

    success, completed, failed = stack_manager.deploy_orchestrated(parallel=True)

    assert success is False
    assert "gco-regional-api-us-west-2" in failed
    assert "gco-monitoring" not in completed
    assert stack_manager._deploy_stacks_parallel.call_count == 2


def test_deploy_orchestration_stops_on_sequential_failure(
    stack_manager: Any,
) -> None:
    stack_manager.list_stacks = MagicMock(
        return_value=["gco-global", "gco-us-east-1", "gco-monitoring"]
    )
    stack_manager.deploy = MagicMock(side_effect=[True, False])
    assert stack_manager.deploy_orchestrated() == (
        False,
        ["gco-global"],
        ["gco-us-east-1"],
    )


def test_parallel_deploy_classifies_results_and_cancels_on_worker_error(
    stack_manager: Any,
) -> None:
    stack_manager.deploy = MagicMock(
        side_effect=lambda **kwargs: kwargs["stack_name"] == "gco-us-east-1"
    )
    started = MagicMock()
    completed = MagicMock()
    successful, failed = stack_manager._deploy_stacks_parallel(
        stacks=["gco-us-east-1", "gco-us-west-2"],
        require_approval=False,
        outputs_file=None,
        parameters=None,
        tags=None,
        progress="events",
        on_stack_start=started,
        on_stack_complete=completed,
        max_workers=2,
        allow_bootstrap=True,
        bootstrap_stacks=None,
        expected_stack_ids=None,
        prepared_change_sets=None,
        authorize_stack=None,
    )
    assert successful == ["gco-us-east-1"]
    assert failed == ["gco-us-west-2"]
    assert started.call_count == 2
    assert completed.call_count == 2

    stack_manager.deploy.side_effect = RuntimeError("worker failed")
    stack_manager.cancel_active_cdk_processes = MagicMock()
    with pytest.raises(RuntimeError, match="worker failed"):
        stack_manager._deploy_stacks_parallel(
            stacks=["gco-us-east-1"],
            require_approval=True,
            outputs_file=None,
            parameters=None,
            tags=None,
            progress="events",
            on_stack_start=None,
            on_stack_complete=None,
            max_workers=1,
            allow_bootstrap=True,
            bootstrap_stacks=None,
            expected_stack_ids=None,
            prepared_change_sets=None,
            authorize_stack=None,
        )
    stack_manager.cancel_active_cdk_processes.assert_called_once_with()


def _ready_destroy_orchestrator(manager: Any, names: list[str]) -> None:
    manager.list_stacks = MagicMock(return_value=names)
    manager._image_registry_destroy_preflight = MagicMock(return_value=True)
    manager.cleanup_orphaned_bastions = MagicMock(return_value=1)
    manager._cleanup_bastion_iam = MagicMock(return_value={"completed_steps": 1})
    manager._cleanup_backup_vault = MagicMock(return_value={"errors": []})
    manager._collect_implicit_log_groups = MagicMock(
        return_value={"gco-global": {"region": "us-east-1", "log_groups": ["group"]}}
    )
    manager._cleanup_implicit_log_groups = MagicMock(return_value={"deleted": ["group"]})
    manager._cleanup_traffic_dial_parameters = MagicMock(return_value={"deleted": []})
    manager.destroy = MagicMock(return_value=True)
    manager._destroy_phase_remaining_stacks = MagicMock(return_value=[])
    thread = MagicMock()
    thread.is_alive.return_value = False
    manager._start_eks_sg_watchdog = MagicMock(return_value=thread)
    manager._cleanup_eks_security_groups = MagicMock(return_value={"errors": []})
    manager._cleanup_cluster_volumes = MagicMock(return_value={"deleted": []})


@pytest.mark.parametrize(
    ("case", "message"),
    [
        ("missing", "target map is incomplete"),
        ("authorizer", "exact stack authorizer"),
        ("identity", "invalid stack identities"),
        ("callback", "both a run token"),
        ("history", "lacks prepared change-set history"),
        ("history_keys", "history does not match"),
    ],
)
def test_destroy_orchestration_rejects_invalid_strict_authority(
    stack_manager: Any,
    case: str,
    message: str,
) -> None:
    stack_manager.list_stacks = MagicMock(return_value=["gco-global"])
    kwargs: dict[str, Any] = {
        "expected_stack_ids": {"gco-global": "arn:stack"},
        "authorize_stack": MagicMock(),
    }
    if case == "missing":
        kwargs["expected_stack_ids"] = {}
    elif case == "authorizer":
        kwargs["authorize_stack"] = None
    elif case == "identity":
        kwargs["expected_stack_ids"] = {"gco-global": "name-only"}
    elif case == "callback":
        kwargs["strict_deployment_token"] = "run"
    elif case == "history":
        kwargs.update(
            strict_deployment_token="run",
            on_change_set_prepared=MagicMock(),
        )
    elif case == "history_keys":
        kwargs.update(
            strict_deployment_token="run",
            on_change_set_prepared=MagicMock(),
            prepared_change_sets={"other": {}},
        )
    with pytest.raises(RuntimeError, match=message):
        stack_manager.destroy_orchestrated(**kwargs)


def test_destroy_orchestration_runs_all_sequential_phases_and_cleanup(
    stack_manager: Any,
) -> None:
    names = [
        "gco-monitoring",
        "gco-regional-api-us-east-1",
        "gco-us-east-1",
        "gco-api-gateway",
        "gco-global",
    ]
    _ready_destroy_orchestrator(stack_manager, names)
    cleanup = MagicMock()
    started = MagicMock()
    completed = MagicMock()

    result = stack_manager.destroy_orchestrated(
        on_stack_start=started,
        on_stack_complete=completed,
        on_cleanup_complete=cleanup,
        retain_volumes=True,
    )

    assert result == (True, names, [])
    assert started.call_count == len(names)
    assert completed.call_count == len(names)
    stack_manager._cleanup_cluster_volumes.assert_called_once_with(
        "gco-us-east-1",
        region=None,
        retain=True,
    )
    stack_manager._cleanup_implicit_log_groups.assert_called_once()
    stack_manager._cleanup_traffic_dial_parameters.assert_called_once()
    cleanup_names = [item.args[0] for item in cleanup.call_args_list]
    assert "dynamic-pvs" in cleanup_names
    assert "traffic-dial-parameters" in cleanup_names


def test_destroy_orchestration_parallel_bridge_failure_stops_regional_phase(
    stack_manager: Any,
) -> None:
    names = [
        "gco-regional-api-us-east-1",
        "gco-regional-api-us-west-2",
        "gco-us-east-1",
        "gco-us-west-2",
    ]
    _ready_destroy_orchestrator(stack_manager, names)
    stack_manager._destroy_stacks_parallel = MagicMock(
        return_value=(
            ["gco-regional-api-us-east-1"],
            ["gco-regional-api-us-west-2"],
        )
    )

    result = stack_manager.destroy_orchestrated(parallel=True)

    assert result[0] is False
    assert result[2] == ["gco-regional-api-us-west-2"]
    stack_manager._start_eks_sg_watchdog.assert_not_called()


def test_destroy_orchestration_registry_refusal_returns_every_stack(
    stack_manager: Any,
) -> None:
    names = ["gco-global", "gco-us-east-1"]
    _ready_destroy_orchestrator(stack_manager, names)
    stack_manager._image_registry_destroy_preflight.return_value = False
    assert stack_manager.destroy_orchestrated() == (False, [], names)


def test_parallel_destroy_classifies_results_and_cancels_on_worker_error(
    stack_manager: Any,
) -> None:
    stack_manager.destroy = MagicMock(
        side_effect=lambda **kwargs: kwargs["stack_name"] == "gco-us-east-1"
    )
    successful, failed = stack_manager._destroy_stacks_parallel(
        stacks=["gco-us-east-1", "gco-us-west-2"],
        force=True,
        on_stack_start=MagicMock(),
        on_stack_complete=MagicMock(),
        max_workers=2,
        expected_stack_ids=None,
        authorize_stack=None,
        allow_bootstrap=True,
        bootstrap_stacks=None,
        prepared_change_sets=None,
    )
    assert successful == ["gco-us-east-1"]
    assert failed == ["gco-us-west-2"]

    stack_manager.destroy.side_effect = RuntimeError("worker failed")
    stack_manager.cancel_active_cdk_processes = MagicMock()
    with pytest.raises(RuntimeError, match="worker failed"):
        stack_manager._destroy_stacks_parallel(
            stacks=["gco-us-east-1"],
            force=True,
            on_stack_start=None,
            on_stack_complete=None,
            max_workers=1,
            expected_stack_ids=None,
            authorize_stack=None,
            allow_bootstrap=True,
            bootstrap_stacks=None,
            prepared_change_sets=None,
        )
    stack_manager.cancel_active_cdk_processes.assert_called_once_with()


# ---------------------------------------------------------------------------
# Stack cleanup ownership, pagination, and reporting contracts
# ---------------------------------------------------------------------------


def test_posix_file_lock_dispatches_through_native_flock() -> None:
    import cli.stacks as stacks

    handle = MagicMock()
    handle.fileno.return_value = 19
    with (
        patch.object(stacks.os, "name", "posix"),
        patch.object(stacks, "_acquire_posix_flock") as acquire,
    ):
        stacks._acquire_file_lock(handle, exclusive=True, purpose="configuration")
    acquire.assert_called_once_with(
        19,
        lock_name=handle.name,
        exclusive=True,
        purpose="configuration",
    )


def test_windows_config_lock_releases_successful_lock(tmp_path: Path) -> None:
    import cli.stacks as stacks

    handle = MagicMock()
    with (
        patch.object(stacks.os, "name", "nt"),
        patch.object(Path, "open", return_value=handle),
        patch.object(stacks, "_acquire_file_lock") as acquire,
        patch.object(stacks, "_release_file_lock") as release,
        stacks._config_process_lock(tmp_path),
    ):
        acquire.assert_called_once_with(
            handle,
            exclusive=True,
            purpose="configuration",
        )
    release.assert_called_once_with(handle)
    handle.close.assert_called_once_with()


def test_find_cdk_uses_secondary_global_location(stack_manager: Any) -> None:
    with (
        patch.object(Path, "is_file", return_value=False),
        patch("subprocess.run", side_effect=subprocess.CalledProcessError(1, "which")),
        patch(
            "os.path.exists",
            side_effect=lambda path: path == os.path.expanduser("~/.npm-global/bin/cdk"),
        ),
    ):
        assert stack_manager._find_cdk() == os.path.expanduser("~/.npm-global/bin/cdk")


def test_kubectl_builder_surfaces_dependency_install_failure(
    stack_manager: Any,
    tmp_path: Path,
) -> None:
    import cli.stacks as stacks

    source, _build = stacks._KUBECTL_CDK_ASSET.paths(tmp_path)
    source.mkdir(parents=True)
    (source / "handler.py").write_text("handler", encoding="utf-8")
    (source / "requirements.txt").write_text("dependency==1", encoding="utf-8")
    (source / "manifests").mkdir()
    staging = tmp_path / "failed-pip-staging"
    staging.mkdir()

    def prepare(*_args: Any, builder: Any, **_kwargs: Any) -> bool:
        builder(staging)
        return True

    with (
        patch.object(stacks, "_prepare_lambda_asset", side_effect=prepare),
        patch.object(
            stacks.subprocess,
            "run",
            return_value=SimpleNamespace(returncode=1, stderr="resolver failed"),
        ),
        pytest.raises(RuntimeError, match="dependency installation failed"),
    ):
        stack_manager._build_kubectl_lambda()


def test_inference_builder_rejects_wrong_installed_npm_version(
    stack_manager: Any,
    tmp_path: Path,
) -> None:
    import cli.stacks as stacks

    source, _build = stacks._INFERENCE_STREAMING_CDK_ASSET.paths(tmp_path)
    source.mkdir(parents=True)
    for name in stacks._INFERENCE_STREAMING_CDK_ASSET.source_inputs or ():
        (source / name).write_text("{}", encoding="utf-8")
    (source / "package.json").write_text(
        json.dumps({"packageManager": "npm@10.9.2"}), encoding="utf-8"
    )
    staging = tmp_path / "wrong-npm-staging"
    staging.mkdir()

    def prepare(*_args: Any, builder: Any, **_kwargs: Any) -> bool:
        builder(staging)
        return True

    with (
        patch.object(stacks, "_prepare_lambda_asset", side_effect=prepare),
        patch.object(stacks.shutil, "which", return_value="/usr/bin/npm"),
        patch.object(
            stacks.subprocess,
            "run",
            return_value=SimpleNamespace(returncode=0, stdout="9.9.9\n", stderr=""),
        ),
        pytest.raises(RuntimeError, match="found 9.9.9"),
    ):
        stack_manager._build_inference_streaming_proxy_lambda()


def test_run_cdk_resolves_executable_lazily(stack_manager: Any) -> None:
    process = MagicMock(pid=81, returncode=0)
    process.communicate.return_value = ("output", "")
    stack_manager._cdk_path = None
    stack_manager._ensure_cdk_toolchain = MagicMock()
    stack_manager._get_python_path = MagicMock(return_value="pythonpath")
    stack_manager._find_cdk = MagicMock(return_value="/local/cdk")
    with patch("cli.stacks.subprocess.Popen", return_value=process) as popen:
        result = stack_manager._run_cdk(["bootstrap"], capture_output=True)
    assert result.stdout == "output"
    assert stack_manager._cdk_path == "/local/cdk"
    popen.assert_called_once()


def test_image_registry_inventory_falls_back_when_import_is_unavailable(
    stack_manager: Any,
) -> None:
    real_import = builtins.__import__

    def fail_images(name: str, *args: Any, **kwargs: Any) -> Any:
        if name == "cli.images":
            raise ImportError("images unavailable")
        return real_import(name, *args, **kwargs)

    with patch("builtins.__import__", side_effect=fail_images):
        assert stack_manager._build_image_registry_inventory() == {
            "repo_count": 0,
            "tag_count": 0,
            "total_bytes": 0,
            "endpoint_refs": 0,
            "job_refs": 0,
        }


def test_describe_stack_target_propagates_non_missing_client_error(
    stack_manager: Any,
) -> None:
    stack_manager._get_destroy_region = MagicMock(return_value="us-east-1")
    client = MagicMock()
    client.describe_stacks.side_effect = _client_error("AccessDenied", "denied")
    with (
        patch("boto3.client", return_value=client),
        pytest.raises(ClientError),
    ):
        stack_manager._describe_stack_target("gco-global")


def test_regional_api_validation_fails_closed_on_metadata_errors(
    stack_manager: Any,
) -> None:
    import cli.stacks as stacks

    with patch.object(
        stacks,
        "_known_cloudformation_regions",
        side_effect=RuntimeError("metadata unavailable"),
    ):
        assert stack_manager._validated_regional_api_region("gco-regional-api-us-east-1") is None

    stack_manager._validated_regional_api_region = MagicMock(return_value="us-west-2")
    stack_manager._configured_regional_api_regions = MagicMock(
        return_value=(frozenset({"us-east-1"}), "aws")
    )
    with patch.object(
        stacks,
        "validated_deployment_partition",
        side_effect=ValueError("unknown region"),
    ):
        assert stack_manager._get_orphan_regional_api_region("gco-regional-api-us-west-2") is None


def test_strict_analytics_dependency_requires_api_identity(stack_manager: Any) -> None:
    stack_manager._stack_exists_in_cloudformation = MagicMock(return_value=True)
    with pytest.raises(RuntimeError, match="authoritative target state"):
        stack_manager._remove_api_gateway_analytics_dependency(
            expected_stack_ids={"gco-analytics": "arn:analytics"}
        )


def test_analytics_dependency_exception_rechecks_actual_imports(
    stack_manager: Any,
) -> None:
    stack_manager._stack_exists_in_cloudformation = MagicMock(return_value=True)
    stack_manager._api_gateway_imports_from_analytics = MagicMock(side_effect=[True, False])
    with patch("cli.stacks.get_analytics_config", side_effect=RuntimeError("bad config")):
        assert stack_manager._remove_api_gateway_analytics_dependency() is True

    stack_manager._api_gateway_imports_from_analytics.side_effect = [
        True,
        RuntimeError("cannot recheck"),
    ]
    with patch("cli.stacks.get_analytics_config", side_effect=RuntimeError("bad config")):
        assert stack_manager._remove_api_gateway_analytics_dependency() is False


def test_is_bootstrapped_caches_false_after_missing_stack(stack_manager: Any) -> None:
    client = MagicMock()
    client.describe_stacks.side_effect = _client_error("ValidationError", "missing")
    with patch("boto3.client", return_value=client):
        assert stack_manager.is_bootstrapped("eu-west-1") is False
        assert stack_manager.is_bootstrapped("eu-west-1") is False
    client.describe_stacks.assert_called_once()


def test_change_set_inspection_retries_until_visible(stack_manager: Any) -> None:
    response = _strict_change_set_response()
    stack_manager._get_deploy_region = MagicMock(return_value="us-east-1")
    stack_manager._describe_stack_target = MagicMock(
        return_value=(
            "us-east-1",
            MagicMock(),
            {"StackStatus": "REVIEW_IN_PROGRESS"},
        )
    )
    stack_manager._wait_for_stack_settle = MagicMock(return_value="CREATE_COMPLETE")
    client = MagicMock()
    client.describe_change_set.side_effect = [
        _client_error("ChangeSetNotFound"),
        response,
    ]
    with (
        patch("boto3.client", return_value=client),
        patch("cli.stacks.time.sleep") as sleep,
    ):
        assert (
            stack_manager._execute_prepared_change_set(
                stack_name="gco-global",
                change_set_name="change",
                expected_stack_id=None,
                expected_tags={"Project": "gco"},
                prepared_change_sets={},
                preparation_succeeded=True,
                authorize_stack=None,
                on_change_set_prepared=MagicMock(),
                allow_noop=False,
                timeout=1,
            )
            is True
        )
    sleep.assert_called_once()
    assert client.describe_change_set.call_count == 2


def test_strict_deploy_orchestration_validates_each_unique_region(
    stack_manager: Any,
) -> None:
    names = ["gco-global", "gco-api-gateway"]
    stack_manager.list_stacks = MagicMock(return_value=names)
    stack_manager._get_deploy_region = MagicMock(return_value="us-east-1")
    stack_manager._validate_bootstrap_stack = MagicMock()
    stack_manager._describe_stack_target = MagicMock(
        return_value=("us-east-1", MagicMock(), {"StackId": "arn:stack"})
    )
    stack_manager.deploy = MagicMock(return_value=True)
    authorize = MagicMock()

    assert stack_manager.deploy_orchestrated(
        allow_bootstrap=False,
        bootstrap_stacks={"us-east-1": {"stack_id": "toolkit"}},
        expected_stack_ids=dict.fromkeys(names, "arn:stack"),
        prepared_change_sets={name: {} for name in names},
        authorize_stack=authorize,
        strict_deployment_token="run",
        on_change_set_prepared=MagicMock(),
    ) == (True, names, [])
    stack_manager._validate_bootstrap_stack.assert_called_once()
    assert authorize.call_count == 2


def test_parallel_workers_swallow_output_directory_cleanup_errors(
    stack_manager: Any,
) -> None:
    stack_manager.deploy = MagicMock(return_value=True)
    with patch("shutil.rmtree", side_effect=OSError("busy")):
        assert stack_manager._deploy_stacks_parallel(
            stacks=["gco-us-east-1"],
            require_approval=True,
            outputs_file=None,
            parameters=None,
            tags=None,
            progress="events",
            on_stack_start=None,
            on_stack_complete=None,
            max_workers=1,
            allow_bootstrap=True,
            bootstrap_stacks=None,
            expected_stack_ids=None,
            prepared_change_sets=None,
            authorize_stack=None,
        ) == (["gco-us-east-1"], [])

    stack_manager.destroy = MagicMock(return_value=True)
    with patch("shutil.rmtree", side_effect=OSError("busy")):
        assert stack_manager._destroy_stacks_parallel(
            stacks=["gco-us-east-1"],
            force=True,
            on_stack_start=None,
            on_stack_complete=None,
            max_workers=1,
            expected_stack_ids=None,
            authorize_stack=None,
            allow_bootstrap=True,
            bootstrap_stacks=None,
            prepared_change_sets=None,
        ) == (["gco-us-east-1"], [])


def test_strict_resource_resolution_tracks_exact_vpc_cluster_and_security_group(
    stack_manager: Any,
) -> None:
    cfn = MagicMock()
    paginator = MagicMock()
    paginator.paginate.return_value = [
        {
            "StackResourceSummaries": [
                {"ResourceType": "AWS::EC2::VPC", "PhysicalResourceId": "vpc-1"}
            ]
        },
        {
            "StackResourceSummaries": [
                {
                    "ResourceType": "AWS::EKS::Cluster",
                    "PhysicalResourceId": "gco-us-east-1",
                }
            ]
        },
    ]
    cfn.get_paginator.return_value = paginator
    target = (
        "us-east-1",
        cfn,
        {"StackId": "arn:regional"},
    )
    stack_manager._describe_stack_target = MagicMock(side_effect=[target, None])
    stack_manager._get_deploy_region = MagicMock(return_value="us-east-1")
    eks_client = MagicMock()
    eks_client.describe_cluster.return_value = {
        "cluster": {
            "name": "gco-us-east-1",
            "resourcesVpcConfig": {
                "vpcId": "vpc-1",
                "clusterSecurityGroupId": "sg-1",
            },
        }
    }
    authorize = MagicMock()

    with patch("boto3.client", return_value=eks_client):
        resolved = stack_manager._resolve_strict_teardown_resources(
            stacks=["gco-us-east-1", "gco-global"],
            regional_stacks=["gco-us-east-1"],
            expected_stack_ids={
                "gco-us-east-1": "arn:regional",
                "gco-global": None,
            },
            authorize_stack=authorize,
        )

    assert resolved["gco-us-east-1"]["vpc_id"] == "vpc-1"
    assert resolved["gco-us-east-1"]["cluster_security_group_id"] == "sg-1"
    authorize.assert_called_once_with("gco-us-east-1", "us-east-1", "arn:regional")


def test_backup_vault_cleanup_handles_absence_success_and_partial_failure(
    stack_manager: Any,
) -> None:
    from cli.stacks import StackManager

    manager = StackManager(
        SimpleNamespace(project_name="gco", global_region="us-east-2"),
        project_root=stack_manager.project_root,
    )
    manager._describe_stack_target = MagicMock(return_value=None)
    assert _REAL_CLEANUP_BACKUP_VAULT(manager)["status"] == "stack-absent"

    cfn = MagicMock()
    resource_paginator = MagicMock()
    resource_paginator.paginate.return_value = [
        {
            "StackResourceSummaries": [
                {
                    "ResourceType": "AWS::Backup::BackupVault",
                    "PhysicalResourceId": (
                        "arn:aws:backup:us-east-2:123456789012:backup-vault:gco-vault"
                    ),
                    "LogicalResourceId": "Vault",
                }
            ]
        }
    ]
    cfn.get_paginator.return_value = resource_paginator
    manager._describe_stack_target.return_value = (
        "us-east-2",
        cfn,
        {"StackId": "arn:global"},
    )
    backup = MagicMock()
    backup.describe_backup_vault.return_value = {
        "BackupVaultArn": ("arn:aws:backup:us-east-2:123456789012:backup-vault:gco-vault")
    }
    recovery_paginator = MagicMock()
    recovery_paginator.paginate.return_value = [
        {
            "RecoveryPoints": [
                {},
                {"RecoveryPointArn": "arn:point:one"},
                {"RecoveryPointArn": "arn:point:two"},
            ]
        }
    ]
    backup.get_paginator.return_value = recovery_paginator
    backup.delete_recovery_point.side_effect = [None, RuntimeError("locked")]
    authorize = MagicMock()

    with patch("boto3.client", return_value=backup):
        outcome = _REAL_CLEANUP_BACKUP_VAULT(
            manager,
            authorize_stack=authorize,
        )

    assert outcome["status"] == "partial"
    assert outcome["deleted_recovery_points"] == 1
    assert len(outcome["errors"]) == 1
    authorize.assert_called_once_with("gco-global", "us-east-2", "arn:global")


def test_cleanup_orphaned_bastions_filters_stacks_and_parallelizes(
    stack_manager: Any,
) -> None:
    stack_manager._cleanup_orphaned_bastions = MagicMock(return_value=1)
    assert (
        stack_manager.cleanup_orphaned_bastions(["gco-global", "gco-us-east-1"], parallel=False)
        == 1
    )
    stack_manager._cleanup_orphaned_bastions.assert_called_once_with(
        "gco-us-east-1",
        region=None,
        vpc_id=None,
        fail_closed=False,
    )

    stack_manager._cleanup_orphaned_bastions.reset_mock()
    assert (
        stack_manager.cleanup_orphaned_bastions(["gco-us-east-1", "gco-us-west-2"], parallel=True)
        == 2
    )
    assert stack_manager._cleanup_orphaned_bastions.call_count == 2


def test_orphaned_bastion_cleanup_terminates_owned_instances(
    stack_manager: Any,
) -> None:
    from cli.stacks import StackManager

    stack_manager._get_deploy_region = MagicMock(return_value="us-east-1")
    stack_manager._wait_for_bastion_network_interfaces = MagicMock(return_value={"eni-1"})
    ec2 = MagicMock()
    ec2.describe_vpcs.return_value = {"Vpcs": [{"VpcId": "vpc-1"}]}
    ec2.describe_instances.return_value = {
        "Reservations": [
            {
                "Instances": [
                    {
                        "InstanceId": "i-owned",
                        "Tags": [
                            {"Key": "gco:project", "Value": "gco"},
                            {"Key": "Name", "Value": "ignored"},
                        ],
                        "NetworkInterfaces": [
                            {
                                "NetworkInterfaceId": "eni-1",
                                "Attachment": {
                                    "DeviceIndex": 0,
                                    "DeleteOnTermination": True,
                                },
                            }
                        ],
                    },
                    {
                        "InstanceId": "i-foreign",
                        "Tags": [{"Key": "Project", "Value": "other"}],
                    },
                ]
            }
        ]
    }
    waiter = MagicMock()
    ec2.get_waiter.return_value = waiter

    with patch("boto3.client", return_value=ec2):
        assert (
            StackManager._cleanup_orphaned_bastions(
                stack_manager,
                "gco-us-east-1",
            )
            == 1
        )

    ec2.terminate_instances.assert_called_once_with(InstanceIds=["i-owned"])
    waiter.wait.assert_called_once()
    stack_manager._wait_for_bastion_network_interfaces.assert_called_once_with(ec2, ["eni-1"])


def test_wait_for_bastion_network_interfaces_handles_absent_and_detached() -> None:
    from cli.stacks import StackManager

    ec2 = MagicMock()

    def describe_network_interfaces(*, NetworkInterfaceIds: list[str]) -> dict[str, Any]:
        if NetworkInterfaceIds == ["eni-missing"]:
            raise _client_error("InvalidNetworkInterfaceID.NotFound")
        return {"NetworkInterfaces": [{"Status": "available"}]}

    ec2.describe_network_interfaces.side_effect = describe_network_interfaces
    with patch("cli.stacks.time.sleep"):
        assert (
            StackManager._wait_for_bastion_network_interfaces(
                ec2,
                ["eni-missing", "eni-detached"],
                timeout_seconds=1,
                poll_interval_seconds=0.01,
            )
            == set()
        )
    ec2.delete_network_interface.assert_called_once_with(NetworkInterfaceId="eni-detached")


def test_implicit_log_group_collection_and_cleanup_cover_all_outcomes(
    stack_manager: Any,
) -> None:
    cfn = MagicMock()
    paginator = MagicMock()
    paginator.paginate.return_value = [
        {
            "StackResourceSummaries": [
                {
                    "ResourceType": "AWS::Lambda::Function",
                    "PhysicalResourceId": "fn-one",
                }
            ]
        },
        {
            "StackResourceSummaries": [
                {
                    "ResourceType": "AWS::EKS::Cluster",
                    "PhysicalResourceId": "cluster-one",
                }
            ]
        },
    ]
    cfn.get_paginator.return_value = paginator
    stack_manager._describe_stack_target = MagicMock(
        side_effect=[
            ("us-east-1", cfn, {"StackId": "arn:stack"}),
            None,
            RuntimeError("cannot describe"),
        ]
    )

    collected = _REAL_COLLECT_IMPLICIT_LOG_GROUPS(
        stack_manager,
        ["gco-us-east-1", "gco-absent", "gco-error"],
    )
    assert "/aws/lambda/fn-one" in collected["gco-us-east-1"]["log_groups"]
    assert len(collected["gco-us-east-1"]["log_groups"]) == 6

    logs = MagicMock()
    logs.delete_log_group.side_effect = [
        None,
        _client_error("ResourceNotFoundException"),
        _client_error("AccessDeniedException"),
        RuntimeError("offline"),
    ]
    details = {
        "gco-us-east-1": {
            "region": "us-east-1",
            "log_groups": ["a", "b", "c", "d"],
        }
    }
    with patch("boto3.client", return_value=logs) as client:
        outcome = _REAL_CLEANUP_IMPLICIT_LOG_GROUPS(
            stack_manager,
            details,
            ["gco-missing", "gco-us-east-1"],
        )
    assert outcome["deleted"] == ["us-east-1:a"]
    assert outcome["missing"] == ["us-east-1:b"]
    assert len(outcome["errors"]) == 2
    client.assert_called_once_with("logs", region_name="us-east-1")


def test_orphaned_eni_summary_classifies_and_deletes_only_safe_interfaces(
    stack_manager: Any,
) -> None:
    ec2 = MagicMock()
    ec2.describe_vpcs.return_value = {"Vpcs": [{"VpcId": "vpc-1"}]}
    ec2.describe_network_interfaces.return_value = {
        "NetworkInterfaces": [
            {
                "NetworkInterfaceId": "eni-ga",
                "InterfaceType": "global_accelerator_managed",
                "Status": "in-use",
                "RequesterManaged": True,
            },
            {
                "NetworkInterfaceId": "eni-elb",
                "Description": "ELB app/demo",
                "Status": "in-use",
                "RequesterManaged": True,
            },
            {
                "NetworkInterfaceId": "eni-eks",
                "Description": "Amazon EKS interface",
                "Status": "available",
                "RequesterManaged": False,
            },
            {
                "NetworkInterfaceId": "eni-other",
                "Status": "available",
                "RequesterManaged": False,
            },
        ]
    }
    ec2.delete_network_interface.side_effect = [None, RuntimeError("busy")]
    with patch("boto3.client", return_value=ec2):
        summary = stack_manager._summarize_orphaned_enis("gco-us-east-1")
    assert summary == {
        "global_accelerator": 1,
        "elb": 1,
        "eks": 1,
        "other": 1,
        "deleted": 1,
        "vpcs": 1,
    }


def test_eks_security_group_cleanup_handles_absent_blocked_deleted_and_errors(
    stack_manager: Any,
) -> None:
    ec2 = MagicMock()
    ec2.describe_security_groups.side_effect = _client_error("InvalidGroup.NotFound")
    with patch("boto3.client", return_value=ec2):
        outcome = _REAL_CLEANUP_EKS_SECURITY_GROUPS(
            stack_manager,
            "gco-us-east-1",
            security_group_id="sg-missing",
        )
    assert outcome["absent"] is True

    ec2 = MagicMock()
    ec2.describe_security_groups.return_value = {
        "SecurityGroups": [
            {"GroupId": "sg-blocked", "GroupName": "blocked", "VpcId": "vpc-1"},
            {"GroupId": "sg-deleted", "GroupName": "deleted", "VpcId": "vpc-1"},
            {"GroupId": "sg-missing", "GroupName": "missing", "VpcId": "vpc-1"},
            {"GroupId": "sg-error", "GroupName": "error", "VpcId": "vpc-1"},
        ]
    }
    ec2.describe_network_interfaces.side_effect = [
        {"NetworkInterfaces": [{"NetworkInterfaceId": "eni-1"}]},
        {"NetworkInterfaces": []},
        {"NetworkInterfaces": []},
        {"NetworkInterfaces": []},
    ]
    ec2.delete_security_group.side_effect = [
        None,
        _client_error("InvalidGroup.NotFound"),
        RuntimeError("cannot delete"),
    ]
    with patch("boto3.client", return_value=ec2):
        outcome = _REAL_CLEANUP_EKS_SECURITY_GROUPS(
            stack_manager,
            "gco-us-east-1",
            vpc_id="vpc-1",
        )
    assert outcome["inspected"] == 4
    assert outcome["blocked_by_enis"][0]["network_interface_ids"] == ["eni-1"]
    assert outcome["deleted"][0]["group_id"] == "sg-deleted"
    assert outcome["absent"] is True
    assert len(outcome["errors"]) == 1


def test_cluster_volume_cleanup_reports_retained_blocked_deleted_and_failed(
    stack_manager: Any,
) -> None:
    volume = {
        "VolumeId": "vol-retained",
        "Size": 10,
        "VolumeType": "gp3",
        "AvailabilityZone": "us-east-1a",
        "Tags": [
            {
                "Key": "kubernetes.io/created-for/pvc/name",
                "Value": "database",
            }
        ],
    }
    eks = MagicMock()
    eks.describe_cluster.side_effect = _client_error("ResourceNotFoundException")
    ec2 = MagicMock()
    paginator = MagicMock()
    paginator.paginate.return_value = [{"Volumes": [volume]}]
    ec2.get_paginator.return_value = paginator
    stack_manager._price_surviving_volumes = MagicMock()

    with patch("boto3.client", side_effect=[eks, ec2]):
        retained = _REAL_CLEANUP_CLUSTER_VOLUMES(
            stack_manager,
            "gco-us-east-1",
            retain=True,
        )
    assert retained["surviving"][0]["reason"] == "retained-by-request"

    volumes = [
        {**volume, "VolumeId": "vol-deleted"},
        {**volume, "VolumeId": "vol-blocked"},
        {**volume, "VolumeId": "vol-error"},
    ]
    eks = MagicMock()
    eks.describe_cluster.side_effect = _client_error("ResourceNotFoundException")
    ec2 = MagicMock()
    paginator = MagicMock()
    paginator.paginate.return_value = [{"Volumes": volumes}]
    ec2.get_paginator.return_value = paginator
    ec2.delete_volume.side_effect = [None, RuntimeError("delete failed")]
    stack_manager._volume_delete_blocked = MagicMock(
        side_effect=[None, "volume-has-attachments", None]
    )
    with patch("boto3.client", side_effect=[eks, ec2]):
        outcome = _REAL_CLEANUP_CLUSTER_VOLUMES(
            stack_manager,
            "gco-us-east-1",
        )
    assert [item["volume_id"] for item in outcome["deleted"]] == ["vol-deleted"]
    assert outcome["surviving"][0]["reason"] == "volume-has-attachments"
    assert outcome["errors"][0]["volume_id"] == "vol-error"


def test_volume_pricing_walks_products_terms_and_dimensions() -> None:
    from cli.stacks import StackManager

    pricing = MagicMock()
    pricing.get_products.return_value = {
        "PriceList": [
            json.dumps({"terms": {"OnDemand": {}}}),
            json.dumps(
                {
                    "terms": {
                        "OnDemand": {
                            "term-one": {"priceDimensions": {}},
                            "term-two": {
                                "priceDimensions": {"dimension": {"pricePerUnit": {"USD": "0.08"}}}
                            },
                        }
                    }
                }
            ),
        ]
    }
    with patch("boto3.client", return_value=pricing):
        assert StackManager._volume_storage_price_per_gib_month("us-east-1", "gp3") == 0.08


def test_surviving_volume_pricing_and_record_rendering(stack_manager: Any) -> None:
    import cli.stacks as stacks

    outcome = {
        "region": "us-east-1",
        "surviving": [
            {"volume_type": "gp3", "size_gib": 10},
            {"volume_type": "gp3", "size_gib": 5},
        ],
    }
    stack_manager._volume_storage_price_per_gib_month = MagicMock(return_value=0.08)
    stack_manager._price_surviving_volumes(outcome)
    assert outcome["monthly_cost_usd"] == 1.2
    stack_manager._volume_storage_price_per_gib_month.assert_called_once_with("us-east-1", "gp3")

    volume = {
        "Tags": [
            {"Key": "other", "Value": "ignored"},
            {"Key": "kubernetes.io/created-for/pvc/name", "Value": "pvc-one"},
        ]
    }
    assert stacks._volume_pvc_name(volume) == "pvc-one"
    assert stacks._volume_pvc_name({"Tags": []}) is None
    assert (
        stacks._describe_volume_record(
            {
                "volume_id": "vol-1",
                "size_gib": None,
                "volume_type": "gp3",
                "availability_zone": "us-east-1a",
                "pvc": "pvc-one",
            }
        )
        == "vol-1 (unknown size gp3, us-east-1a, pvc=pvc-one)"
    )


def test_feature_config_updates_global_and_regional_values_atomically(
    tmp_path: Path,
) -> None:
    import cli.stacks as stacks

    path = tmp_path / "cdk.json"
    path.write_text("{}", encoding="utf-8")
    with patch.object(stacks, "_find_cdk_json", return_value=path):
        stacks._update_feature_config(
            "feature",
            {"enabled": False, "size": None, "mode": "fast"},
            {"enabled": True, "size": 1},
        )
        stacks._update_feature_config(
            "feature",
            {"enabled": True, "size": 3, "skip": None},
            {"enabled": False},
            region="us-west-2",
        )
    document = json.loads(path.read_text(encoding="utf-8"))
    assert document["context"]["feature"] == {
        "enabled": False,
        "size": 1,
        "mode": "fast",
    }
    assert document["context"]["feature_regions"]["us-west-2"] == {
        "enabled": True,
        "size": 3,
    }


def test_observability_config_helpers_delegate_to_shared_feature_engine() -> None:
    import cli.stacks as stacks

    with patch.object(stacks, "_get_feature_config", return_value={"enabled": True}) as get:
        assert stacks.get_cluster_observability_config() == {"enabled": True}
    get.assert_called_once_with(
        "cluster_observability",
        stacks._CLUSTER_OBSERVABILITY_DEFAULTS,
    )
    with patch.object(stacks, "_update_feature_config") as update:
        stacks.update_cluster_observability_config({"enabled": False})
    update.assert_called_once_with(
        "cluster_observability",
        {"enabled": False},
        stacks._CLUSTER_OBSERVABILITY_DEFAULTS,
    )


# ---------------------------------------------------------------------------
# Residual CDK synthesis exits and multi-item loops
# ---------------------------------------------------------------------------


def test_regional_stack_rich_ga_synthesis_completes_optional_branches() -> None:
    from tests.test_regional_stack_feature_gap_coverage import _synthesize

    stack, template = _synthesize(
        feature_rich=True,
        global_accelerator=True,
        logical_name="coverage-100-regional-rich-ga",
    )
    assert stack.global_accelerator_enabled is True
    assert stack.aurora_cluster is not None
    assert template.find_resources("AWS::StepFunctions::StateMachine")


def test_regional_stack_unknown_kubernetes_version_uses_custom_version() -> None:
    from tests import test_regional_stack_feature_gap_coverage as helper

    original = helper.FeatureConfig.get_cluster_config

    def unknown_version(self: Any, region: str | None = None) -> Any:
        value = original(self, region)
        value.kubernetes_version = "99.99"
        return value

    with patch.object(helper.FeatureConfig, "get_cluster_config", unknown_version):
        stack, template = helper._synthesize(
            feature_rich=False,
            global_accelerator=False,
            logical_name="coverage-100-regional-custom-k8s-version",
        )
    template.has_resource_properties("AWS::EKS::Cluster", {"Version": "99.99"})


def test_global_vector_ingest_synthesis_completes_notification_wiring() -> None:
    import aws_cdk as cdk
    from aws_cdk import assertions

    from gco.stacks.global_stack import GCOGlobalStack
    from tests.test_cdk_stacks import MockConfigLoader

    class VectorConfig(MockConfigLoader):
        def get_vector_store_enabled(self) -> bool:
            return True

        def get_vector_store_config(self) -> dict[str, Any]:
            return {
                **super().get_vector_store_config(),
                "enabled": True,
            }

    app = cdk.App()
    stack = GCOGlobalStack(
        app,
        "coverage-100-global-vector-ingest",
        config=VectorConfig(app),
        env=cdk.Environment(account="123456789012", region="us-east-2"),
    )
    template = assertions.Template.from_stack(stack)
    assert any(
        logical_id.startswith("VectorIngestFunction")
        for logical_id in template.find_resources("AWS::Lambda::Function")
    )
    assert template.find_resources("Custom::S3BucketNotifications")


def test_monitoring_composite_alarm_loops_across_three_regions() -> None:
    import aws_cdk as cdk
    from aws_cdk import assertions

    from gco.stacks.monitoring_stack import GCOMonitoringStack
    from tests.test_monitoring_stack import (
        MockConfigLoader,
        create_mock_global_stack,
        create_mock_regional_stack,
    )

    stack = GCOMonitoringStack(
        cdk.App(),
        "Coverage100MonitoringThreeRegions",
        config=MockConfigLoader(cost_monitoring_enabled=False),
        global_stack=create_mock_global_stack(),
        regional_stacks=[
            create_mock_regional_stack("us-east-1"),
            create_mock_regional_stack("us-west-2"),
            create_mock_regional_stack("eu-west-1"),
        ],
        api_gateway_stack=None,
        env=cdk.Environment(account="123456789012", region="us-east-2"),
    )
    template = assertions.Template.from_stack(stack)
    assert len(template.find_resources("AWS::CloudWatch::CompositeAlarm")) == 3


# ---------------------------------------------------------------------------
# Final executable baseline branches: identities, strict loops, and errors
# ---------------------------------------------------------------------------


def test_windows_lock_propagates_non_contention_error() -> None:
    import cli.stacks as stacks

    handle = MagicMock()
    handle.fileno.return_value = 7
    msvcrt = SimpleNamespace(
        LK_NBLCK=1,
        locking=MagicMock(side_effect=OSError(errno.EBADF, "bad handle")),
    )
    with (
        patch.object(stacks.os, "name", "nt"),
        patch.dict(sys.modules, {"msvcrt": msvcrt}),
        pytest.raises(OSError, match="bad handle"),
    ):
        stacks._acquire_file_lock(handle, exclusive=True, purpose="asset")


def test_posix_config_lock_closes_descriptor_after_acquisition_error(
    tmp_path: Path,
) -> None:
    import cli.stacks as stacks

    with (
        patch.object(stacks.os, "open", return_value=31),
        patch.object(stacks.os, "close") as close,
        patch.object(stacks, "_acquire_posix_flock", side_effect=OSError("busy")),
        pytest.raises(stacks.ConfigMutationLockError, match="could not lock"),
        stacks._config_process_lock(tmp_path),
    ):
        pass
    close.assert_called_once_with(31)


def test_find_cdk_prefers_repository_local_cli(stack_manager: Any) -> None:
    local = stack_manager.project_root / "node_modules" / ".bin" / "cdk"
    with patch.object(Path, "is_file", return_value=True):
        assert stack_manager._find_cdk() == str(local)


def test_inference_builder_wraps_npm_version_probe_error(
    stack_manager: Any,
    tmp_path: Path,
) -> None:
    import cli.stacks as stacks

    source, _build = stacks._INFERENCE_STREAMING_CDK_ASSET.paths(tmp_path)
    source.mkdir(parents=True)
    for name in stacks._INFERENCE_STREAMING_CDK_ASSET.source_inputs or ():
        (source / name).write_text("{}", encoding="utf-8")
    (source / "package.json").write_text(
        json.dumps({"packageManager": "npm@10.9.2"}), encoding="utf-8"
    )
    staging = tmp_path / "npm-probe-staging"
    staging.mkdir()

    def prepare(*_args: Any, builder: Any, **_kwargs: Any) -> bool:
        builder(staging)
        return True

    with (
        patch.object(stacks, "_prepare_lambda_asset", side_effect=prepare),
        patch.object(stacks.shutil, "which", return_value="/usr/bin/npm"),
        patch.object(stacks.subprocess, "run", side_effect=OSError("cannot execute")),
        pytest.raises(RuntimeError, match="Unable to verify"),
    ):
        stack_manager._build_inference_streaming_proxy_lambda()


def test_strict_deploy_rejects_disappeared_expected_stack(stack_manager: Any) -> None:
    import cli.stacks as stacks

    _ready_deploy_manager(stack_manager)
    stack_manager._describe_stack_target = MagicMock(return_value=None)
    kwargs = _strict_deploy_kwargs()
    kwargs["stack_name"] = "gco-global"
    kwargs["expected_stack_ids"] = {"gco-global": "arn:expected"}
    kwargs["prepared_change_sets"] = {"gco-global": {}}
    with (
        patch.object(stacks, "_detect_container_runtime", return_value="docker"),
        pytest.raises(RuntimeError, match="absent; refusing recreation"),
    ):
        stack_manager.deploy(**kwargs)


def test_public_analytics_destroy_requires_configuration_file(
    stack_manager: Any,
) -> None:
    with (
        patch("cli.stacks._find_cdk_json", return_value=None),
        pytest.raises(RuntimeError, match="cdk.json not found"),
    ):
        stack_manager.destroy(stack_name="gco-analytics")


def test_describe_stack_target_rejects_changed_exact_identity(
    stack_manager: Any,
) -> None:
    stack_manager._get_destroy_region = MagicMock(return_value="us-east-1")
    client = MagicMock()
    client.describe_stacks.return_value = {
        "Stacks": [
            {
                "StackName": "gco-global",
                "StackId": "arn:replacement",
                "StackStatus": "CREATE_COMPLETE",
            }
        ]
    }
    with (
        patch("boto3.client", return_value=client),
        pytest.raises(RuntimeError, match="identity changed"),
    ):
        stack_manager._describe_stack_target(
            "gco-global",
            expected_stack_id="arn:expected",
        )


def test_api_gateway_import_detection_walks_exports_and_import_pages(
    stack_manager: Any,
) -> None:
    stack_manager._get_deploy_region = MagicMock(return_value="us-east-2")
    export_paginator = MagicMock()
    export_paginator.paginate.return_value = [
        {
            "Exports": [
                {
                    "ExportingStackId": "arn:stack/gco-analytics/id",
                    "Name": "First",
                },
                {
                    "ExportingStackId": "arn:stack/other/id",
                    "Name": "Other",
                },
            ]
        },
        {
            "Exports": [
                {
                    "ExportingStackId": "arn:stack/gco-analytics/id",
                    "Name": "Second",
                }
            ]
        },
    ]
    import_paginator = MagicMock()
    import_paginator.paginate.side_effect = [
        [{"Imports": ["other-stack"]}],
        [{"Imports": []}, {"Imports": ["gco-api-gateway"]}],
    ]
    client = MagicMock()
    client.get_paginator.side_effect = [export_paginator, import_paginator]
    with patch("boto3.client", return_value=client):
        assert stack_manager._api_gateway_imports_from_analytics() is True


def _invoke_empty_change_set(
    stack_manager: Any,
    *,
    target: Any,
    authorize_stack: Any,
) -> bool:
    response = _strict_change_set_response(status="FAILED")
    response["StatusReason"] = "No updates are to be performed."
    stack_id = response["StackId"]
    stack_manager._get_deploy_region = MagicMock(return_value="us-east-1")
    stack_manager._describe_stack_target = MagicMock(return_value=target)
    client = MagicMock()
    client.describe_change_set.return_value = response
    with patch("boto3.client", return_value=client):
        return stack_manager._execute_prepared_change_set(
            stack_name="gco-global",
            change_set_name="change",
            expected_stack_id=stack_id,
            expected_tags={"Project": "gco"},
            prepared_change_sets={},
            preparation_succeeded=True,
            authorize_stack=authorize_stack,
            on_change_set_prepared=MagicMock(),
            allow_noop=True,
            timeout=1,
        )


def test_empty_change_set_requires_healthy_target_and_authorizer(
    stack_manager: Any,
) -> None:
    with pytest.raises(RuntimeError, match="no healthy exact stack"):
        _invoke_empty_change_set(
            stack_manager,
            target=None,
            authorize_stack=MagicMock(),
        )
    healthy = (
        "us-east-1",
        MagicMock(),
        {"StackStatus": "UPDATE_COMPLETE"},
    )
    with pytest.raises(RuntimeError, match="lacks exact authorization"):
        _invoke_empty_change_set(
            stack_manager,
            target=healthy,
            authorize_stack=None,
        )
    authorize = MagicMock()
    assert _invoke_empty_change_set(
        stack_manager,
        target=healthy,
        authorize_stack=authorize,
    )
    authorize.assert_called_once()


def test_bootstrap_without_account_or_region_uses_default_target(
    stack_manager: Any,
) -> None:
    stack_manager._run_cdk = MagicMock(return_value=SimpleNamespace(returncode=0))
    assert stack_manager.bootstrap() is True
    stack_manager._run_cdk.assert_called_once_with(["bootstrap"])


def test_ensure_bootstrapped_reports_bootstrap_failure(stack_manager: Any) -> None:
    stack_manager.is_bootstrapped = MagicMock(return_value=False)
    stack_manager.bootstrap = MagicMock(return_value=False)
    assert stack_manager.ensure_bootstrapped("us-east-1") is False


@pytest.mark.parametrize(
    ("case", "message"),
    [
        ("region", "Could not resolve deploy Region"),
        ("toolkit", "checkpointed CDKToolkit identity"),
        ("stack", "absent; refusing recreation"),
    ],
)
def test_strict_deploy_orchestration_fails_closed_during_identity_walk(
    stack_manager: Any,
    case: str,
    message: str,
) -> None:
    stack_manager.list_stacks = MagicMock(return_value=["gco-global"])
    stack_manager._get_deploy_region = MagicMock(return_value="us-east-1")
    stack_manager._validate_bootstrap_stack = MagicMock()
    stack_manager._describe_stack_target = MagicMock(return_value=None)
    kwargs = {
        "allow_bootstrap": False,
        "bootstrap_stacks": {"us-east-1": {"stack_id": "toolkit"}},
        "expected_stack_ids": {"gco-global": "arn:expected"},
        "prepared_change_sets": {"gco-global": {}},
        "authorize_stack": MagicMock(),
        "strict_deployment_token": "run",
        "on_change_set_prepared": MagicMock(),
    }
    if case == "region":
        stack_manager._get_deploy_region.return_value = None
    elif case == "toolkit":
        kwargs["bootstrap_stacks"] = {}
    with pytest.raises(RuntimeError, match=message):
        stack_manager.deploy_orchestrated(**kwargs)


def test_strict_resource_resolution_recovers_security_group_after_cluster_delete(
    stack_manager: Any,
) -> None:
    cfn = MagicMock()
    paginator = MagicMock()
    paginator.paginate.return_value = [
        {
            "StackResourceSummaries": [
                {"ResourceType": "AWS::EC2::VPC", "PhysicalResourceId": "vpc-1"},
                {
                    "ResourceType": "AWS::EKS::Cluster",
                    "PhysicalResourceId": "gco-us-east-1",
                },
            ]
        }
    ]
    cfn.get_paginator.return_value = paginator
    stack_manager._describe_stack_target = MagicMock(
        return_value=("us-east-1", cfn, {"StackId": "arn:regional"})
    )
    stack_manager._get_deploy_region = MagicMock(return_value="us-east-1")
    eks = MagicMock()
    eks.describe_cluster.side_effect = _client_error("ResourceNotFoundException")
    ec2 = MagicMock()
    ec2.describe_security_groups.return_value = {"SecurityGroups": [{"GroupId": "sg-recovered"}]}
    with patch("boto3.client", side_effect=[eks, ec2]):
        resolved = stack_manager._resolve_strict_teardown_resources(
            stacks=["gco-us-east-1"],
            regional_stacks=["gco-us-east-1"],
            expected_stack_ids={"gco-us-east-1": "arn:regional"},
            authorize_stack=MagicMock(),
        )
    assert resolved["gco-us-east-1"]["cluster_security_group_id"] == "sg-recovered"


def test_destroy_orchestration_blocks_on_remaining_regional_api_stack(
    stack_manager: Any,
) -> None:
    names = ["gco-regional-api-us-east-1"]
    _ready_destroy_orchestrator(stack_manager, names)
    stack_manager._destroy_phase_remaining_stacks.side_effect = [
        [],
        ["gco-regional-api-us-east-1"],
    ]
    assert stack_manager.destroy_orchestrated() == (
        False,
        ["gco-regional-api-us-east-1"],
        ["gco-regional-api-us-east-1"],
    )


def test_strict_destroy_orchestration_records_watchdog_failures(
    stack_manager: Any,
) -> None:
    names = ["gco-us-east-1", "gco-us-west-2"]
    _ready_destroy_orchestrator(stack_manager, names)
    stack_manager._resolve_strict_teardown_resources = MagicMock(
        return_value={
            name: {
                "region": name.removeprefix("gco-"),
                "vpc_id": f"vpc-{index}",
                "cluster_security_group_id": f"sg-{index}",
            }
            for index, name in enumerate(names)
        }
    )
    joined_error = MagicMock()
    joined_error.join.side_effect = RuntimeError("join failed")
    alive = MagicMock()
    alive.is_alive.return_value = True
    stack_manager._start_eks_sg_watchdog.side_effect = [joined_error, alive]
    cleanup = MagicMock()
    result = stack_manager.destroy_orchestrated(
        expected_stack_ids={name: f"arn:{name}" for name in names},
        authorize_stack=MagicMock(),
        on_cleanup_complete=cleanup,
    )
    assert result[0] is False
    assert set(result[2]) == set(names)
    assert cleanup.call_count >= 4


def test_destroy_orchestration_blocks_on_remaining_pre_regional_stack(
    stack_manager: Any,
) -> None:
    names = ["gco-global", "gco-api-gateway"]
    _ready_destroy_orchestrator(stack_manager, names)
    stack_manager._destroy_phase_remaining_stacks.side_effect = [
        [],
        [],
        ["gco-api-gateway"],
    ]
    success, completed, failed = stack_manager.destroy_orchestrated()
    assert success is False
    assert completed == ["gco-api-gateway"]
    assert failed == ["gco-api-gateway"]


def test_backup_vault_cleanup_rejects_untrusted_resource_shapes(
    stack_manager: Any,
) -> None:
    manager = _StackManager(
        SimpleNamespace(project_name="gco", global_region="us-east-2"),
        project_root=stack_manager.project_root,
    )
    cfn = MagicMock()
    paginator = MagicMock()
    cfn.get_paginator.return_value = paginator
    manager._describe_stack_target = MagicMock(
        return_value=("us-west-2", cfn, {"StackId": "arn:global"})
    )
    assert _REAL_CLEANUP_BACKUP_VAULT(manager)["status"] == "failed"

    manager._describe_stack_target.return_value = (
        "us-east-2",
        cfn,
        {"StackId": "arn:global"},
    )
    paginator.paginate.return_value = [{"StackResourceSummaries": []}]
    assert _REAL_CLEANUP_BACKUP_VAULT(manager)["status"] == "vault-resource-absent"

    paginator.paginate.return_value = [
        {
            "StackResourceSummaries": [
                {
                    "ResourceType": "AWS::Backup::BackupVault",
                    "PhysicalResourceId": "arn:aws:backup:us-east-2:123:wrong:gco",
                }
            ]
        }
    ]
    assert _REAL_CLEANUP_BACKUP_VAULT(manager)["status"] == "failed"


def test_backup_vault_cleanup_validates_service_arn_and_plain_name(
    stack_manager: Any,
) -> None:
    manager = _StackManager(
        SimpleNamespace(project_name="gco", global_region="us-east-2"),
        project_root=stack_manager.project_root,
    )
    cfn = MagicMock()
    paginator = MagicMock()
    paginator.paginate.return_value = [
        {
            "StackResourceSummaries": [
                {
                    "ResourceType": "AWS::Backup::BackupVault",
                    "PhysicalResourceId": "plain-vault",
                }
            ]
        }
    ]
    cfn.get_paginator.return_value = paginator
    manager._describe_stack_target = MagicMock(
        return_value=("us-east-2", cfn, {"StackId": "arn:global"})
    )
    backup = MagicMock()
    backup.describe_backup_vault.return_value = {
        "BackupVaultArn": ("arn:aws:backup:us-east-2:123456789012:backup-vault:plain-vault")
    }
    recovery = MagicMock()
    recovery.paginate.return_value = []
    backup.get_paginator.return_value = recovery
    with patch("boto3.client", return_value=backup):
        assert _REAL_CLEANUP_BACKUP_VAULT(manager)["status"] == "completed"

    backup.describe_backup_vault.return_value = {
        "BackupVaultArn": "arn:aws:backup:us-west-2:123:backup-vault:plain-vault"
    }
    with patch("boto3.client", return_value=backup):
        assert _REAL_CLEANUP_BACKUP_VAULT(manager)["status"] == "failed"


def test_cleanup_orphaned_bastions_default_strict_and_empty_inputs(
    stack_manager: Any,
) -> None:
    stack_manager.list_stacks = MagicMock(return_value=[])
    assert stack_manager.cleanup_orphaned_bastions() == 0

    stack_manager._cleanup_orphaned_bastions = MagicMock(return_value=1)
    targets = {"gco-us-east-1": {"region": "us-east-1", "vpc_id": "vpc-1"}}
    assert (
        stack_manager.cleanup_orphaned_bastions(
            ["gco-us-east-1", "gco-us-west-2"],
            resource_targets=targets,
        )
        == 1
    )
    stack_manager._cleanup_orphaned_bastions.assert_called_once_with(
        "gco-us-east-1",
        region="us-east-1",
        vpc_id="vpc-1",
        fail_closed=True,
    )


def test_orphaned_eni_summary_degrades_on_lookup_and_describe_errors(
    stack_manager: Any,
) -> None:
    with patch("boto3.client", side_effect=RuntimeError("ec2 unavailable")):
        assert stack_manager._summarize_orphaned_enis("gco-us-east-1")["vpcs"] == 0

    ec2 = MagicMock()
    ec2.describe_vpcs.return_value = {"Vpcs": [{"VpcId": "vpc-1"}]}
    ec2.describe_network_interfaces.side_effect = RuntimeError("cannot inspect")
    with patch("boto3.client", return_value=ec2):
        summary = stack_manager._summarize_orphaned_enis("gco-us-east-1")
    assert summary["vpcs"] == 1
    assert summary["deleted"] == 0


def test_eks_security_group_cleanup_rejects_identity_and_delete_errors(
    stack_manager: Any,
) -> None:
    ec2 = MagicMock()
    ec2.describe_security_groups.return_value = {
        "SecurityGroups": [{"GroupId": "sg-other", "GroupName": "changed", "VpcId": "vpc-1"}]
    }
    with patch("boto3.client", return_value=ec2):
        outcome = _REAL_CLEANUP_EKS_SECURITY_GROUPS(
            stack_manager,
            "gco-us-east-1",
            security_group_id="sg-expected",
        )
    assert "changed security-group identity" in outcome["errors"][0]["error"]

    ec2 = MagicMock()
    ec2.describe_security_groups.return_value = {
        "SecurityGroups": [{"GroupId": "sg-one", "GroupName": "one", "VpcId": "vpc-other"}]
    }
    with patch("boto3.client", return_value=ec2):
        outcome = _REAL_CLEANUP_EKS_SECURITY_GROUPS(
            stack_manager,
            "gco-us-east-1",
            vpc_id="vpc-expected",
        )
    assert "exact VPC" in outcome["errors"][0]["error"]

    ec2 = MagicMock()
    ec2.describe_security_groups.return_value = {
        "SecurityGroups": [{"GroupId": "sg-one", "GroupName": "one", "VpcId": "vpc-1"}]
    }
    ec2.describe_network_interfaces.return_value = {"NetworkInterfaces": []}
    ec2.delete_security_group.side_effect = _client_error("AccessDeniedException")
    with patch("boto3.client", return_value=ec2):
        outcome = _REAL_CLEANUP_EKS_SECURITY_GROUPS(
            stack_manager,
            "gco-us-east-1",
        )
    assert "AccessDeniedException" in outcome["errors"][0]["error"]


def test_cluster_volume_cleanup_skips_live_cluster_and_records_client_error(
    stack_manager: Any,
) -> None:
    eks = MagicMock()
    with patch("boto3.client", return_value=eks):
        live = _REAL_CLEANUP_CLUSTER_VOLUMES(stack_manager, "gco-us-east-1")
    assert live["cluster_present"] is True

    eks = MagicMock()
    eks.describe_cluster.side_effect = _client_error("ResourceNotFoundException")
    ec2 = MagicMock()
    paginator = MagicMock()
    paginator.paginate.return_value = [
        {
            "Volumes": [
                {
                    "VolumeId": "vol-denied",
                    "Size": 1,
                    "VolumeType": "gp3",
                    "Tags": [],
                }
            ]
        }
    ]
    ec2.get_paginator.return_value = paginator
    ec2.delete_volume.side_effect = _client_error("AccessDeniedException")
    stack_manager._volume_delete_blocked = MagicMock(return_value=None)
    stack_manager._price_surviving_volumes = MagicMock()
    with patch("boto3.client", side_effect=[eks, ec2]):
        outcome = _REAL_CLEANUP_CLUSTER_VOLUMES(stack_manager, "gco-us-east-1")
    assert "AccessDeniedException" in outcome["errors"][0]["error"]


def test_regional_stack_vector_store_disabled_skips_manifest_replacements() -> None:
    from tests import test_regional_stack_feature_gap_coverage as helper

    with patch.object(helper.FeatureConfig, "get_vector_store_enabled", return_value=False):
        stack, _template = helper._synthesize(
            feature_rich=False,
            global_accelerator=False,
            logical_name="coverage-100-regional-vector-disabled",
        )
    assert stack.config.get_vector_store_enabled() is False


def test_regional_stack_single_lbc_chart_skips_non_lbc_chain() -> None:
    from gco.stacks import regional_stack as regional
    from tests import test_regional_stack_feature_gap_coverage as helper

    with patch.object(
        regional,
        "_load_helm_chart_order",
        return_value=["aws-load-balancer-controller"],
    ):
        stack, _template = helper._synthesize(
            feature_rich=False,
            global_accelerator=False,
            logical_name="coverage-100-regional-single-lbc",
        )
    assert stack.helm_teardown_state_machine is not None


def test_global_vector_ingest_without_discoverable_notification_handler() -> None:
    import aws_cdk as cdk
    from aws_cdk import assertions
    from constructs import Node

    from gco.stacks.global_stack import GCOGlobalStack
    from tests.test_cdk_stacks import MockConfigLoader

    class VectorConfig(MockConfigLoader):
        def get_vector_store_enabled(self) -> bool:
            return True

        def get_vector_store_config(self) -> dict[str, Any]:
            return {**super().get_vector_store_config(), "enabled": True}

    original = Node.try_find_child

    def hide_notification_handler(self: Any, child_id: str) -> Any:
        if child_id == "BucketNotificationsHandler050a0587b7544547bf325f094a3db834":
            return None
        return original(self, child_id)

    app = cdk.App()
    with patch.object(Node, "try_find_child", hide_notification_handler):
        stack = GCOGlobalStack(
            app,
            "coverage-100-global-vector-no-handler",
            config=VectorConfig(app),
            env=cdk.Environment(account="123456789012", region="us-east-2"),
        )
        template = assertions.Template.from_stack(stack)
    assert template.find_resources("Custom::S3BucketNotifications")


# ---------------------------------------------------------------------------
# Last executable lines from the authoritative baseline
# ---------------------------------------------------------------------------


def test_missing_change_set_noop_requires_authorizer(stack_manager: Any) -> None:
    response = _strict_change_set_response()
    stack_id = response["StackId"]
    stack_manager._get_deploy_region = MagicMock(return_value="us-east-1")
    stack_manager._describe_stack_target = MagicMock(
        return_value=(
            "us-east-1",
            MagicMock(),
            {"StackStatus": "UPDATE_COMPLETE"},
        )
    )
    client = MagicMock()
    client.describe_change_set.side_effect = _client_error("ChangeSetNotFound")
    with (
        patch("boto3.client", return_value=client),
        pytest.raises(RuntimeError, match="lacks exact authorization"),
    ):
        stack_manager._execute_prepared_change_set(
            stack_name="gco-global",
            change_set_name="change",
            expected_stack_id=stack_id,
            expected_tags=None,
            prepared_change_sets={},
            preparation_succeeded=True,
            authorize_stack=None,
            on_change_set_prepared=MagicMock(),
            allow_noop=True,
            timeout=1,
        )


def test_strict_resource_resolution_propagates_non_missing_eks_error(
    stack_manager: Any,
) -> None:
    cfn = MagicMock()
    paginator = MagicMock()
    paginator.paginate.return_value = [
        {
            "StackResourceSummaries": [
                {"ResourceType": "AWS::EC2::VPC", "PhysicalResourceId": "vpc-1"},
                {
                    "ResourceType": "AWS::EKS::Cluster",
                    "PhysicalResourceId": "gco-us-east-1",
                },
            ]
        }
    ]
    cfn.get_paginator.return_value = paginator
    stack_manager._describe_stack_target = MagicMock(
        return_value=("us-east-1", cfn, {"StackId": "arn:regional"})
    )
    stack_manager._get_deploy_region = MagicMock(return_value="us-east-1")
    eks = MagicMock()
    eks.describe_cluster.side_effect = _client_error("AccessDeniedException")
    with (
        patch("boto3.client", return_value=eks),
        pytest.raises(ClientError),
    ):
        stack_manager._resolve_strict_teardown_resources(
            stacks=["gco-us-east-1"],
            regional_stacks=["gco-us-east-1"],
            expected_stack_ids={"gco-us-east-1": "arn:regional"},
            authorize_stack=MagicMock(),
        )


def test_strict_destroy_includes_checkpointed_stack_absent_from_cdk_graph(
    stack_manager: Any,
) -> None:
    _ready_destroy_orchestrator(stack_manager, [])
    stack_manager._resolve_strict_teardown_resources = MagicMock(return_value={})
    stack_manager._collect_implicit_log_groups = MagicMock(return_value={})
    assert stack_manager.destroy_orchestrated(
        expected_stack_ids={"gco-global": None},
        authorize_stack=MagicMock(),
    ) == (True, ["gco-global"], [])
    stack_manager.destroy.assert_called_once()


def test_destroy_orchestration_blocks_on_remaining_monitoring_stack(
    stack_manager: Any,
) -> None:
    names = ["gco-monitoring"]
    _ready_destroy_orchestrator(stack_manager, names)
    stack_manager._destroy_phase_remaining_stacks.return_value = names
    assert stack_manager.destroy_orchestrated() == (
        False,
        ["gco-monitoring"],
        ["gco-monitoring"],
    )


def test_destroy_orchestration_blocks_on_real_regional_api_phase(
    stack_manager: Any,
) -> None:
    bridge = "gco-regional-api-us-east-1"
    names = [bridge, "gco-us-east-1"]
    _ready_destroy_orchestrator(stack_manager, names)
    stack_manager._destroy_phase_remaining_stacks.side_effect = [[], [bridge]]
    assert stack_manager.destroy_orchestrated() == (
        False,
        [bridge],
        [bridge],
    )


class _TruthyEmptyIdentity:
    def __bool__(self) -> bool:
        return True

    def __str__(self) -> str:
        return ""


def _backup_manager_with_resources(
    stack_manager: Any,
    resources: list[dict[str, Any]],
) -> tuple[Any, Any]:
    manager = _StackManager(
        SimpleNamespace(project_name="gco", global_region="us-east-2"),
        project_root=stack_manager.project_root,
    )
    cfn = MagicMock()
    paginator = MagicMock()
    paginator.paginate.return_value = [{"StackResourceSummaries": resources}]
    cfn.get_paginator.return_value = paginator
    manager._describe_stack_target = MagicMock(
        return_value=("us-east-2", cfn, {"StackId": "arn:global"})
    )
    return manager, cfn


def test_backup_vault_cleanup_rejects_ambiguous_and_empty_physical_ids(
    stack_manager: Any,
) -> None:
    resource = {
        "ResourceType": "AWS::Backup::BackupVault",
        "PhysicalResourceId": "vault-one",
    }
    manager, _cfn = _backup_manager_with_resources(
        stack_manager,
        [resource, {**resource, "PhysicalResourceId": "vault-two"}],
    )
    assert _REAL_CLEANUP_BACKUP_VAULT(manager)["status"] == "failed"

    manager, _cfn = _backup_manager_with_resources(
        stack_manager,
        [
            {
                "ResourceType": "AWS::Backup::BackupVault",
                "PhysicalResourceId": _TruthyEmptyIdentity(),
            }
        ],
    )
    assert _REAL_CLEANUP_BACKUP_VAULT(manager)["status"] == "failed"


def test_backup_vault_cleanup_rejects_changed_physical_arn(
    stack_manager: Any,
) -> None:
    physical = "arn:aws:backup:us-east-2:123:backup-vault:vault-one"
    manager, _cfn = _backup_manager_with_resources(
        stack_manager,
        [
            {
                "ResourceType": "AWS::Backup::BackupVault",
                "PhysicalResourceId": physical,
            }
        ],
    )
    backup = MagicMock()
    backup.describe_backup_vault.return_value = {
        "BackupVaultArn": "arn:aws:backup:us-east-2:999:backup-vault:vault-one"
    }
    with patch("boto3.client", return_value=backup):
        assert _REAL_CLEANUP_BACKUP_VAULT(manager)["status"] == "failed"


def test_eks_security_group_cleanup_propagates_describe_access_denial(
    stack_manager: Any,
) -> None:
    ec2 = MagicMock()
    ec2.describe_security_groups.side_effect = _client_error("AccessDeniedException")
    with patch("boto3.client", return_value=ec2):
        outcome = _REAL_CLEANUP_EKS_SECURITY_GROUPS(
            stack_manager,
            "gco-us-east-1",
            security_group_id="sg-one",
        )
    assert "AccessDeniedException" in outcome["errors"][0]["error"]


def test_cluster_volume_cleanup_records_non_missing_cluster_error(
    stack_manager: Any,
) -> None:
    eks = MagicMock()
    eks.describe_cluster.side_effect = _client_error("AccessDeniedException")
    stack_manager._price_surviving_volumes = MagicMock()
    with patch("boto3.client", return_value=eks):
        outcome = _REAL_CLEANUP_CLUSTER_VOLUMES(stack_manager, "gco-us-east-1")
    assert "AccessDeniedException" in outcome["errors"][0]["error"]


def test_publish_failure_without_previous_asset_does_not_invent_rollback(
    tmp_path: Path,
) -> None:
    import cli.stacks as stacks

    staging = tmp_path / ".asset.staging"
    staging.mkdir()
    with (
        patch.object(stacks.os, "replace", side_effect=OSError("publish failed")),
        pytest.raises(OSError, match="publish failed"),
    ):
        stacks._publish_staged_asset(staging, tmp_path / "asset")


def test_windows_config_lock_wraps_sidecar_open_failure(tmp_path: Path) -> None:
    import cli.stacks as stacks

    with (
        patch.object(stacks.os, "name", "nt"),
        patch.object(Path, "open", side_effect=OSError("read only")),
        pytest.raises(stacks.ConfigMutationLockError, match="could not lock"),
        stacks._config_process_lock(tmp_path),
    ):
        pass


def test_deploy_diagnostics_complete_without_actionable_stack_status(
    stack_manager: Any,
) -> None:
    stack_manager._get_deploy_region = MagicMock(return_value="us-east-1")
    client = MagicMock()
    client.describe_stack_events.return_value = {"StackEvents": []}
    client.describe_stacks.return_value = {"Stacks": [{"StackStatus": "CREATE_COMPLETE"}]}
    with patch("boto3.client", return_value=client):
        stack_manager._diagnose_deploy_failure("gco-global")


def test_sync_lambda_sources_walks_multiple_targets(
    stack_manager: Any,
    tmp_path: Path,
) -> None:
    import cli.stacks as stacks

    source = tmp_path / "shared.py"
    source.write_text("shared", encoding="utf-8")
    targets = [tmp_path / "one" / "shared.py", tmp_path / "two" / "shared.py"]
    for target in targets:
        target.parent.mkdir()
    mapping = {"shared.py": tuple(str(target.relative_to(tmp_path)) for target in targets)}
    with patch.object(stacks, "LAMBDA_SHARED_SOURCE_TARGETS", mapping):
        stack_manager._sync_lambda_sources()
    assert [target.read_text(encoding="utf-8") for target in targets] == [
        "shared",
        "shared",
    ]


def test_terminate_cdk_process_posix_graceful_exit(stack_manager: Any) -> None:
    process = MagicMock(pid=123)
    process.poll.return_value = None
    with (
        patch("cli.stacks.os.name", "posix"),
        patch("cli.stacks.os.killpg") as killpg,
    ):
        stack_manager._terminate_cdk_process(process)
    killpg.assert_called_once()
    process.wait.assert_called_once_with(timeout=30)


def test_wait_for_delete_convergence_times_out_after_confirmed_delete_start(
    stack_manager: Any,
) -> None:
    stack_manager._stack_exists_in_cloudformation = MagicMock(return_value=True)
    stack_manager._get_stack_status = MagicMock(return_value="DELETE_IN_PROGRESS")
    stack_manager._print_stack_delete_heartbeat = MagicMock()
    with (
        patch("cli.stacks.time.monotonic", side_effect=[0.0, 0.0, 2.0]),
        patch("cli.stacks.time.sleep"),
    ):
        assert (
            stack_manager._wait_for_stack_delete_convergence(
                "gco-global",
                timeout=1,
                poll_interval=0.1,
                heartbeat_interval=0.1,
            )
            is False
        )


def test_strict_deploy_orchestration_walks_two_regions(
    stack_manager: Any,
) -> None:
    names = ["gco-global", "gco-us-west-2"]
    stack_manager.list_stacks = MagicMock(return_value=names)
    stack_manager._get_deploy_region = MagicMock(
        side_effect=lambda name: "us-east-1" if name.endswith("-global") else "us-west-2"
    )
    stack_manager._validate_bootstrap_stack = MagicMock()
    stack_manager._describe_stack_target = MagicMock(return_value=None)
    stack_manager.deploy = MagicMock(return_value=True)
    assert stack_manager.deploy_orchestrated(
        allow_bootstrap=False,
        bootstrap_stacks={"us-east-1": {}, "us-west-2": {}},
        expected_stack_ids=dict.fromkeys(names),
        prepared_change_sets={name: {} for name in names},
        authorize_stack=MagicMock(),
        strict_deployment_token="run",
        on_change_set_prepared=MagicMock(),
    ) == (True, names, [])
    assert stack_manager._validate_bootstrap_stack.call_count == 2


def test_strict_destroy_orchestration_runs_clean_watchdog_path(
    stack_manager: Any,
) -> None:
    name = "gco-us-east-1"
    _ready_destroy_orchestrator(stack_manager, [name])
    stack_manager._resolve_strict_teardown_resources = MagicMock(
        return_value={
            name: {
                "region": "us-east-1",
                "vpc_id": "vpc-1",
                "cluster_security_group_id": "sg-1",
            }
        }
    )
    thread = MagicMock()
    thread.is_alive.return_value = False
    stack_manager._start_eks_sg_watchdog.return_value = thread
    assert stack_manager.destroy_orchestrated(
        expected_stack_ids={name: f"arn:{name}"},
        authorize_stack=MagicMock(),
    ) == (True, [name], [])
    stack_manager._cleanup_eks_security_groups.assert_called_once()


def test_wait_for_bastion_network_interfaces_empty_input_is_immediate() -> None:
    from cli.stacks import StackManager

    ec2 = MagicMock()
    assert StackManager._wait_for_bastion_network_interfaces(ec2, []) == set()
    ec2.describe_network_interfaces.assert_not_called()


def test_orphaned_eni_summary_reports_deleted_and_retained_counts(capsys: Any) -> None:
    from cli.stacks import StackManager

    StackManager._print_orphaned_eni_summary(
        "gco-us-east-1",
        {
            "global_accelerator": 1,
            "elb": 0,
            "eks": 0,
            "other": 1,
            "deleted": 1,
        },
    )
    output = capsys.readouterr().out
    assert "Removed 1 detached" in output
    assert "1 still held by AWS" in output


def test_volume_helpers_render_minimal_records() -> None:
    import cli.stacks as stacks

    assert stacks._describe_volume_record({"volume_id": "vol-minimal"}) == (
        "vol-minimal (unknown size)"
    )
    assert (
        stacks._describe_volume_record({"volume_id": "vol-sized", "size_gib": 5})
        == "vol-sized (5 GiB)"
    )


# ---------------------------------------------------------------------------
# Feasible residual branch arcs (loop re-entry and empty-option paths)
# ---------------------------------------------------------------------------


def test_sync_lambda_sources_skips_missing_target_parent_then_continues(
    stack_manager: Any,
    tmp_path: Path,
) -> None:
    import cli.stacks as stacks

    source = tmp_path / "shared.py"
    source.write_text("shared", encoding="utf-8")
    existing = tmp_path / "existing" / "shared.py"
    existing.parent.mkdir()
    mapping = {
        "shared.py": (
            "missing/shared.py",
            str(existing.relative_to(tmp_path)),
        )
    }
    with patch.object(stacks, "LAMBDA_SHARED_SOURCE_TARGETS", mapping):
        stack_manager._sync_lambda_sources()
    assert existing.read_text(encoding="utf-8") == "shared"


def test_terminate_cdk_process_posix_error_after_process_already_exits(
    stack_manager: Any,
) -> None:
    process = MagicMock(pid=123)
    process.poll.side_effect = [None, 0]
    with (
        patch("cli.stacks.os.name", "posix"),
        patch("cli.stacks.os.killpg", side_effect=OSError("already gone")),
    ):
        stack_manager._terminate_cdk_process(process)
    process.wait.assert_not_called()


def test_deploy_without_stack_selector_leaves_cdk_default_selection(
    stack_manager: Any,
) -> None:
    import cli.stacks as stacks

    _ready_deploy_manager(stack_manager)
    with patch.object(stacks, "_detect_container_runtime", return_value="docker"):
        assert stack_manager.deploy() is True
    assert stack_manager._run_cdk.call_args.args[0] == [
        "deploy",
        "--progress",
        "events",
    ]


def test_destroy_without_stack_selector_leaves_cdk_default_selection(
    stack_manager: Any,
) -> None:
    _ready_destroy_manager(stack_manager)
    assert stack_manager._destroy() is True
    assert stack_manager._run_cdk.call_args.args[0] == ["destroy"]


def test_delete_convergence_skips_duplicate_heartbeat_before_cancellation(
    stack_manager: Any,
) -> None:
    stack_manager._stack_exists_in_cloudformation = MagicMock(return_value=True)
    stack_manager._get_stack_status = MagicMock(return_value="DELETE_IN_PROGRESS")
    stack_manager._print_stack_delete_heartbeat = MagicMock()
    original_event = stack_manager._cdk_cancel_event
    stack_manager._cdk_cancel_event = MagicMock()
    stack_manager._cdk_cancel_event.is_set.side_effect = [False, False, True]
    with (
        patch(
            "cli.stacks.time.monotonic",
            side_effect=[0.0, 0.0, 0.1, 0.2],
        ),
        patch("cli.stacks.time.sleep"),
    ):
        assert (
            stack_manager._wait_for_stack_delete_convergence(
                "gco-global",
                timeout=10,
                poll_interval=0.1,
                heartbeat_interval=5,
            )
            is False
        )
    stack_manager._cdk_cancel_event = original_event
    stack_manager._print_stack_delete_heartbeat.assert_called_once()


def test_missing_change_set_without_live_target_rejects_noop(
    stack_manager: Any,
) -> None:
    response = _strict_change_set_response()
    stack_manager._get_deploy_region = MagicMock(return_value="us-east-1")
    stack_manager._describe_stack_target = MagicMock(return_value=None)
    client = MagicMock()
    client.describe_change_set.side_effect = _client_error("ChangeSetNotFound")
    with (
        patch("boto3.client", return_value=client),
        pytest.raises(RuntimeError, match="did not create"),
    ):
        stack_manager._execute_prepared_change_set(
            stack_name="gco-global",
            change_set_name="change",
            expected_stack_id=response["StackId"],
            expected_tags=None,
            prepared_change_sets={},
            preparation_succeeded=True,
            authorize_stack=MagicMock(),
            on_change_set_prepared=MagicMock(),
            allow_noop=True,
            timeout=1,
        )


def test_ensure_bootstrapped_reuses_existing_cache_mapping(stack_manager: Any) -> None:
    stack_manager._bootstrap_cache = {}
    stack_manager.is_bootstrapped = MagicMock(return_value=False)
    stack_manager.bootstrap = MagicMock(return_value=True)
    assert stack_manager.ensure_bootstrapped("us-east-1") is True
    assert stack_manager._bootstrap_cache == {"us-east-1": True}


def test_strict_resource_resolution_walks_multiple_regional_candidates(
    stack_manager: Any,
) -> None:
    names = ["gco-us-east-1", "gco-us-west-2"]
    clients: dict[str, Any] = {}
    targets: list[Any] = []
    for index, name in enumerate(names):
        cfn = MagicMock()
        paginator = MagicMock()
        paginator.paginate.return_value = [{"StackResourceSummaries": []}]
        cfn.get_paginator.return_value = paginator
        region = name.removeprefix("gco-")
        clients[region] = cfn
        targets.append((region, cfn, {"StackId": f"arn:{index}"}))
    stack_manager._describe_stack_target = MagicMock(side_effect=targets)
    stack_manager._get_deploy_region = MagicMock(side_effect=lambda name: name.removeprefix("gco-"))
    resolved = stack_manager._resolve_strict_teardown_resources(
        stacks=names,
        regional_stacks=names,
        expected_stack_ids={name: f"arn:{index}" for index, name in enumerate(names)},
        authorize_stack=MagicMock(),
    )
    assert set(resolved) == set(names)


def test_strict_resource_resolution_handles_deleted_cluster_without_vpc(
    stack_manager: Any,
) -> None:
    cfn = MagicMock()
    paginator = MagicMock()
    paginator.paginate.return_value = [
        {
            "StackResourceSummaries": [
                {
                    "ResourceType": "AWS::EKS::Cluster",
                    "PhysicalResourceId": "gco-us-east-1",
                }
            ]
        }
    ]
    cfn.get_paginator.return_value = paginator
    stack_manager._describe_stack_target = MagicMock(
        return_value=("us-east-1", cfn, {"StackId": "arn:regional"})
    )
    stack_manager._get_deploy_region = MagicMock(return_value="us-east-1")
    eks = MagicMock()
    eks.describe_cluster.side_effect = _client_error("ResourceNotFoundException")
    with patch("boto3.client", return_value=eks):
        resolved = stack_manager._resolve_strict_teardown_resources(
            stacks=["gco-us-east-1"],
            regional_stacks=["gco-us-east-1"],
            expected_stack_ids={"gco-us-east-1": "arn:regional"},
            authorize_stack=MagicMock(),
        )
    assert "cluster_security_group_id" not in resolved["gco-us-east-1"]


def test_destroy_phase_remaining_loops_record_multiple_unknown_stacks(
    stack_manager: Any,
) -> None:
    stack_manager._stack_exists_in_cloudformation = MagicMock(
        side_effect=[True, RuntimeError("unknown")]
    )
    assert stack_manager._destroy_phase_remaining_stacks(
        "test phase",
        ["gco-one", "gco-two"],
    ) == ["gco-one", "gco-two"]


def test_bastion_cleanup_foreign_instance_continues_to_owned_instance(
    stack_manager: Any,
) -> None:
    stack_manager._get_deploy_region = MagicMock(return_value="us-east-1")
    stack_manager._wait_for_bastion_network_interfaces = MagicMock(return_value=set())
    ec2 = MagicMock()
    ec2.describe_vpcs.return_value = {"Vpcs": [{"VpcId": "vpc-1"}]}
    ec2.describe_instances.return_value = {
        "Reservations": [
            {
                "Instances": [
                    {
                        "InstanceId": "i-foreign",
                        "Tags": [{"Key": "gco:project", "Value": "other"}],
                    },
                    {
                        "InstanceId": "i-owned",
                        "Tags": [{"Key": "gco:project", "Value": "gco"}],
                        "NetworkInterfaces": [],
                    },
                ]
            },
            {"Instances": []},
        ]
    }
    ec2.get_waiter.return_value = MagicMock()
    with patch("boto3.client", return_value=ec2):
        assert (
            _StackManager._cleanup_orphaned_bastions(
                stack_manager,
                "gco-us-east-1",
            )
            == 1
        )
    ec2.terminate_instances.assert_called_once_with(InstanceIds=["i-owned"])


def test_orphaned_eni_summary_without_deleted_interfaces_still_reports_wait(
    capsys: Any,
) -> None:
    from cli.stacks import StackManager

    StackManager._print_orphaned_eni_summary(
        "gco-us-east-1",
        {
            "global_accelerator": 1,
            "elb": 0,
            "eks": 0,
            "other": 0,
            "deleted": 0,
        },
    )
    output = capsys.readouterr().out
    assert "still held by AWS" in output
    assert "Removed" not in output


def test_volume_pricing_returns_none_after_exhausting_unpriced_dimensions() -> None:
    from cli.stacks import StackManager

    pricing = MagicMock()
    pricing.get_products.return_value = {
        "PriceList": [
            json.dumps(
                {
                    "terms": {
                        "OnDemand": {
                            "term": {
                                "priceDimensions": {
                                    "first": {"pricePerUnit": {}},
                                    "second": {"pricePerUnit": {}},
                                }
                            }
                        }
                    }
                }
            )
        ]
    }
    with patch("boto3.client", return_value=pricing):
        assert StackManager._volume_storage_price_per_gib_month("us-east-1", "gp3") is None


def test_feature_config_updates_existing_global_and_regional_blocks(
    tmp_path: Path,
) -> None:
    import cli.stacks as stacks

    path = tmp_path / "cdk.json"
    path.write_text(
        json.dumps(
            {
                "context": {
                    "feature": {"enabled": False},
                    "feature_regions": {"us-east-1": {"enabled": False}},
                }
            }
        ),
        encoding="utf-8",
    )
    with patch.object(stacks, "_find_cdk_json", return_value=path):
        stacks._update_feature_config(
            "feature",
            {"enabled": True, "mode": "existing"},
            {},
        )
        stacks._update_feature_config(
            "feature",
            {"enabled": True, "mode": "regional"},
            {},
            region="us-east-1",
        )
    context = json.loads(path.read_text(encoding="utf-8"))["context"]
    assert context["feature"]["mode"] == "existing"
    assert context["feature_regions"]["us-east-1"]["mode"] == "regional"


def test_regional_stack_without_teardown_resources_completes_manifest_wiring() -> None:
    from gco.stacks import regional_stack as regional
    from tests import test_regional_stack_feature_gap_coverage as helper

    with (
        patch.object(regional.GCORegionalStack, "_create_helm_teardown"),
        patch.object(regional.GCORegionalStack, "_create_ga_deregistration_resource"),
    ):
        stack, _template = helper._synthesize(
            feature_rich=False,
            global_accelerator=False,
            logical_name="coverage-100-regional-no-teardown-resources",
        )
    assert not hasattr(stack, "helm_teardown_resource")
    assert not hasattr(stack, "ga_deregistration_resource")


def test_regional_stack_without_ga_deregistration_uses_direct_teardown_dependency() -> None:
    from gco.stacks import regional_stack as regional
    from tests import test_regional_stack_feature_gap_coverage as helper

    with patch.object(
        regional.GCORegionalStack,
        "_create_ga_deregistration_resource",
    ):
        stack, _template = helper._synthesize(
            feature_rich=False,
            global_accelerator=False,
            logical_name="coverage-100-regional-direct-teardown",
        )
    assert stack.helm_teardown_resource is not None
    assert not hasattr(stack, "ga_deregistration_resource")


# ---------------------------------------------------------------------------
# Final feasible loop false/backedge arcs
# ---------------------------------------------------------------------------


def test_strict_resource_candidate_filter_skips_bridge_before_base_stack(
    stack_manager: Any,
) -> None:
    names = ["gco-regional-api-us-east-1", "gco-us-east-1"]
    cfn = MagicMock()
    paginator = MagicMock()
    paginator.paginate.return_value = [{"StackResourceSummaries": []}]
    cfn.get_paginator.return_value = paginator
    stack_manager._describe_stack_target = MagicMock(
        side_effect=[
            ("us-east-1", cfn, {"StackId": "arn:bridge"}),
            ("us-east-1", cfn, {"StackId": "arn:base"}),
        ]
    )
    stack_manager._get_deploy_region = MagicMock(return_value="us-east-1")
    resolved = stack_manager._resolve_strict_teardown_resources(
        stacks=names,
        regional_stacks=names,
        expected_stack_ids={names[0]: "arn:bridge", names[1]: "arn:base"},
        authorize_stack=MagicMock(),
    )
    assert set(resolved) == {"gco-us-east-1"}


def test_destroy_monitoring_failure_already_in_failed_is_not_duplicated(
    stack_manager: Any,
) -> None:
    name = "gco-monitoring"
    _ready_destroy_orchestrator(stack_manager, [name])
    stack_manager.destroy.return_value = False
    stack_manager._destroy_phase_remaining_stacks.return_value = [name]
    assert stack_manager.destroy_orchestrated() == (False, [], [name])


def test_destroy_regional_api_failure_already_in_failed_is_not_duplicated(
    stack_manager: Any,
) -> None:
    bridge = "gco-regional-api-us-east-1"
    base = "gco-us-east-1"
    _ready_destroy_orchestrator(stack_manager, [bridge, base])
    stack_manager.destroy.return_value = False
    stack_manager._destroy_phase_remaining_stacks.side_effect = [[], [bridge]]
    assert stack_manager.destroy_orchestrated() == (False, [], [bridge])


def test_nonstrict_watchdog_join_error_is_reported_without_failed_stack(
    stack_manager: Any,
) -> None:
    name = "gco-us-east-1"
    _ready_destroy_orchestrator(stack_manager, [name])
    thread = MagicMock()
    thread.join.side_effect = RuntimeError("join failed")
    stack_manager._start_eks_sg_watchdog.return_value = thread
    cleanup = MagicMock()
    assert stack_manager.destroy_orchestrated(on_cleanup_complete=cleanup) == (
        True,
        [name],
        [],
    )
    assert any(
        item.args[0] == "eks-security-group" and item.args[1]["errors"]
        for item in cleanup.call_args_list
    )


def test_destroy_pre_regional_failure_already_in_failed_is_not_duplicated(
    stack_manager: Any,
) -> None:
    name = "gco-global"
    _ready_destroy_orchestrator(stack_manager, [name])
    stack_manager.destroy.return_value = False
    stack_manager._destroy_phase_remaining_stacks.side_effect = [[], [], [name]]
    assert stack_manager.destroy_orchestrated() == (False, [], [name])


def test_bastion_owned_instance_without_id_and_interface_without_id_are_skipped(
    stack_manager: Any,
) -> None:
    stack_manager._get_deploy_region = MagicMock(return_value="us-east-1")
    stack_manager._wait_for_bastion_network_interfaces = MagicMock(return_value=set())
    ec2 = MagicMock()
    ec2.describe_vpcs.return_value = {"Vpcs": [{"VpcId": "vpc-1"}]}
    ec2.describe_instances.return_value = {
        "Reservations": [
            {
                "Instances": [
                    {
                        "Tags": [{"Key": "gco:project", "Value": "gco"}],
                        "NetworkInterfaces": [
                            {
                                "Attachment": {
                                    "DeviceIndex": 0,
                                    "DeleteOnTermination": True,
                                }
                            }
                        ],
                    },
                    {
                        "InstanceId": "i-owned",
                        "Tags": [{"Key": "gco:project", "Value": "gco"}],
                        "NetworkInterfaces": [],
                    },
                ]
            }
        ]
    }
    ec2.get_waiter.return_value = MagicMock()
    with patch("boto3.client", return_value=ec2):
        assert (
            _StackManager._cleanup_orphaned_bastions(
                stack_manager,
                "gco-us-east-1",
            )
            == 1
        )
    stack_manager._wait_for_bastion_network_interfaces.assert_called_once_with(ec2, [])


def test_implicit_log_collection_loops_across_two_successful_stacks(
    stack_manager: Any,
) -> None:
    cfn = MagicMock()
    paginator = MagicMock()
    paginator.paginate.return_value = [
        {
            "StackResourceSummaries": [
                {
                    "ResourceType": "AWS::Lambda::Function",
                    "PhysicalResourceId": "fn",
                }
            ]
        }
    ]
    cfn.get_paginator.return_value = paginator
    stack_manager._describe_stack_target = MagicMock(
        side_effect=[
            ("us-east-1", cfn, {"StackId": "arn:one"}),
            ("us-west-2", cfn, {"StackId": "arn:two"}),
        ]
    )
    collected = _REAL_COLLECT_IMPLICIT_LOG_GROUPS(
        stack_manager,
        ["gco-one", "gco-two"],
    )
    assert set(collected) == {"gco-one", "gco-two"}


def test_implicit_log_collection_continues_after_error_to_next_stack(
    stack_manager: Any,
) -> None:
    cfn = MagicMock()
    paginator = MagicMock()
    paginator.paginate.return_value = [
        {
            "StackResourceSummaries": [
                {
                    "ResourceType": "AWS::Lambda::Function",
                    "PhysicalResourceId": "fn-after-error",
                }
            ]
        }
    ]
    cfn.get_paginator.return_value = paginator
    stack_manager._describe_stack_target = MagicMock(
        side_effect=[
            RuntimeError("first stack unreadable"),
            ("us-east-1", cfn, {"StackId": "arn:second"}),
        ]
    )
    collected = _REAL_COLLECT_IMPLICIT_LOG_GROUPS(
        stack_manager,
        ["gco-error", "gco-second"],
    )
    assert collected == {
        "gco-second": {
            "region": "us-east-1",
            "log_groups": ["/aws/lambda/fn-after-error"],
        }
    }
