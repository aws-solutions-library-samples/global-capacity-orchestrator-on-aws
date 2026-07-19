"""
Extended unit coverage for ``cli/stacks.py``.

Targets the long tail of destroy-flow helpers and supporting AWS
plumbing that the existing test suite doesn't reach:

* ``_read_images_config`` — the cdk.json parser used by the destroy
  preflight, including the missing-file and parse-error fallbacks.
* ``_build_image_registry_inventory`` — aggregation of repo / tag /
  size / reference counts via a mocked ``ImageManager``.
* ``_image_registry_destroy_preflight`` — every refusal/confirmation
  branch.
* ``_stack_exists_in_cloudformation`` and ``_cloudformation_delete_stack``
  — the boto3-shaped helpers used to delete by-name when CDK can't.
* ``_get_destroy_region`` — the deploy-region lookup.
* ``_ensure_analytics_enabled_for_destroy`` /
  ``_restore_analytics_disabled`` — analytics toggle wrappers.
* ``_api_gateway_imports_from_analytics`` — the CloudFormation
  list_exports / list_imports walk.
* ``_cleanup_backup_vault`` — every recovery-point delete path.
* ``cleanup_eks_security_groups`` and the regional cleanup helper —
  EKS-managed SG + orphaned-ENI cleanup.
* ``_start_eks_sg_watchdog`` — the background thread that drives the
  cleanup helper between destroy retries.
"""

from __future__ import annotations

import json
import re
import stat
import subprocess
import threading
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError


@pytest.fixture
def stacks_module() -> Any:
    """Reload cli.stacks so the runtime cache starts fresh."""
    import importlib

    import cli.stacks as stacks_mod

    importlib.reload(stacks_mod)
    yield stacks_mod
    importlib.reload(stacks_mod)


@pytest.fixture
def manager(stacks_module: Any) -> Any:
    """Build a StackManager bound to a MagicMock config."""
    config = MagicMock()
    config.project_name = "gco"
    config.global_region = "us-east-2"
    config.api_gateway_region = "us-east-2"
    config.regions = ["us-east-2"]
    return stacks_module.StackManager(config)


# ---------------------------------------------------------------------------
# _read_images_config
# ---------------------------------------------------------------------------


class TestReadImagesConfig:
    def test_no_cdk_json_returns_defaults(self, manager: Any) -> None:
        with patch("cli.stacks._find_cdk_json", return_value=None):
            result = manager._read_images_config()
        assert result == {"removal_policy": "retain", "empty_on_delete": False}

    def test_unparseable_cdk_json_returns_defaults(self, manager: Any, tmp_path: Any) -> None:
        bad = tmp_path / "cdk.json"
        bad.write_text("{ not valid")
        with patch("cli.stacks._find_cdk_json", return_value=str(bad)):
            result = manager._read_images_config()
        assert result == {"removal_policy": "retain", "empty_on_delete": False}

    def test_destroy_policy_round_trips(self, manager: Any, tmp_path: Any) -> None:
        good = tmp_path / "cdk.json"
        good.write_text(
            json.dumps(
                {
                    "context": {
                        "images": {
                            "removal_policy": "destroy",
                            "empty_on_delete": True,
                        }
                    }
                }
            )
        )
        with patch("cli.stacks._find_cdk_json", return_value=str(good)):
            result = manager._read_images_config()
        assert result == {"removal_policy": "destroy", "empty_on_delete": True}

    def test_unknown_policy_coerced_to_retain(self, manager: Any, tmp_path: Any) -> None:
        bad_policy = tmp_path / "cdk.json"
        bad_policy.write_text(json.dumps({"context": {"images": {"removal_policy": "shred"}}}))
        with patch("cli.stacks._find_cdk_json", return_value=str(bad_policy)):
            result = manager._read_images_config()
        assert result["removal_policy"] == "retain"


# ---------------------------------------------------------------------------
# _build_image_registry_inventory
# ---------------------------------------------------------------------------


class TestBuildImageRegistryInventory:
    def test_aggregates_repos_and_tags(self, manager: Any) -> None:
        fake_mgr = MagicMock()
        fake_mgr.list_repos.return_value = [
            {"name": "gco/svc-a"},
            {"name": "gco/svc-b"},
            {"name": "other/skipped"},
        ]
        fake_mgr.list_tags.side_effect = [
            [
                {"size_bytes": 100},
                {"size_bytes": 200},
            ],
            [{"size_bytes": 300}],
        ]
        fake_mgr._collect_inference_image_refs.return_value = {"a", "b"}
        fake_mgr._collect_recent_job_image_refs.return_value = {"x"}
        with patch("cli.images.ImageManager", return_value=fake_mgr):
            inventory = manager._build_image_registry_inventory()
        assert inventory["repo_count"] == 3
        assert inventory["tag_count"] == 3
        assert inventory["total_bytes"] == 600
        assert inventory["endpoint_refs"] == 2
        assert inventory["job_refs"] == 1

    def test_inference_ref_failure_does_not_break(self, manager: Any) -> None:
        fake_mgr = MagicMock()
        fake_mgr.list_repos.return_value = [{"name": "gco/svc"}]
        fake_mgr.list_tags.return_value = []
        fake_mgr._collect_inference_image_refs.side_effect = RuntimeError("boom")
        fake_mgr._collect_recent_job_image_refs.side_effect = RuntimeError("boom")
        with patch("cli.images.ImageManager", return_value=fake_mgr):
            inventory = manager._build_image_registry_inventory()
        assert inventory["endpoint_refs"] == 0
        assert inventory["job_refs"] == 0

    def test_list_tags_failure_skips_repo(self, manager: Any) -> None:
        fake_mgr = MagicMock()
        fake_mgr.list_repos.return_value = [
            {"name": "gco/a"},
            {"name": "gco/b"},
        ]
        fake_mgr.list_tags.side_effect = [RuntimeError("denied"), [{"size_bytes": 7}]]
        fake_mgr._collect_inference_image_refs.return_value = set()
        fake_mgr._collect_recent_job_image_refs.return_value = set()
        with patch("cli.images.ImageManager", return_value=fake_mgr):
            inventory = manager._build_image_registry_inventory()
        assert inventory["tag_count"] == 1
        assert inventory["total_bytes"] == 7

    def test_list_repos_failure_returns_partial(self, manager: Any) -> None:
        fake_mgr = MagicMock()
        fake_mgr.list_repos.side_effect = RuntimeError("denied")
        with patch("cli.images.ImageManager", return_value=fake_mgr):
            inventory = manager._build_image_registry_inventory()
        assert inventory["repo_count"] == 0


# ---------------------------------------------------------------------------
# _image_registry_destroy_preflight
# ---------------------------------------------------------------------------


class TestImageRegistryDestroyPreflight:
    def test_retain_policy_short_circuits(self, manager: Any) -> None:
        with patch.object(
            manager,
            "_read_images_config",
            return_value={"removal_policy": "retain", "empty_on_delete": False},
        ):
            assert manager._image_registry_destroy_preflight(force=False) is True

    def test_destroy_without_empty_refuses(self, manager: Any, capsys: Any) -> None:
        with patch.object(
            manager,
            "_read_images_config",
            return_value={"removal_policy": "destroy", "empty_on_delete": False},
        ):
            assert manager._image_registry_destroy_preflight(force=False) is False
        captured = capsys.readouterr().out
        assert "gco images cleanup --all" in captured

    def test_destroy_with_empty_force_proceeds(self, manager: Any, capsys: Any) -> None:
        inventory = {
            "repo_count": 2,
            "tag_count": 5,
            "total_bytes": 0,
            "endpoint_refs": 0,
            "job_refs": 0,
        }
        with (
            patch.object(
                manager,
                "_read_images_config",
                return_value={"removal_policy": "destroy", "empty_on_delete": True},
            ),
            patch.object(manager, "_build_image_registry_inventory", return_value=inventory),
        ):
            assert manager._image_registry_destroy_preflight(force=True) is True
        captured = capsys.readouterr().out
        assert "Image registry inventory" in captured

    def test_destroy_non_tty_proceeds_without_prompt(self, manager: Any) -> None:
        inventory = {
            "repo_count": 1,
            "tag_count": 1,
            "total_bytes": 0,
            "endpoint_refs": 0,
            "job_refs": 0,
        }
        with (
            patch.object(
                manager,
                "_read_images_config",
                return_value={"removal_policy": "destroy", "empty_on_delete": True},
            ),
            patch.object(manager, "_build_image_registry_inventory", return_value=inventory),
            patch("cli.stacks.sys.stdin") as mock_stdin,
        ):
            mock_stdin.isatty.return_value = False
            assert manager._image_registry_destroy_preflight(force=False) is True

    def test_destroy_tty_prompt_yes(self, manager: Any) -> None:
        inventory = {
            "repo_count": 1,
            "tag_count": 1,
            "total_bytes": 0,
            "endpoint_refs": 0,
            "job_refs": 0,
        }
        with (
            patch.object(
                manager,
                "_read_images_config",
                return_value={"removal_policy": "destroy", "empty_on_delete": True},
            ),
            patch.object(manager, "_build_image_registry_inventory", return_value=inventory),
            patch("cli.stacks.sys.stdin") as mock_stdin,
            patch("builtins.input", return_value="yes"),
        ):
            mock_stdin.isatty.return_value = True
            assert manager._image_registry_destroy_preflight(force=False) is True

    def test_destroy_tty_prompt_no(self, manager: Any, capsys: Any) -> None:
        inventory = {
            "repo_count": 1,
            "tag_count": 1,
            "total_bytes": 0,
            "endpoint_refs": 0,
            "job_refs": 0,
        }
        with (
            patch.object(
                manager,
                "_read_images_config",
                return_value={"removal_policy": "destroy", "empty_on_delete": True},
            ),
            patch.object(manager, "_build_image_registry_inventory", return_value=inventory),
            patch("cli.stacks.sys.stdin") as mock_stdin,
            patch("builtins.input", return_value="n"),
        ):
            mock_stdin.isatty.return_value = True
            assert manager._image_registry_destroy_preflight(force=False) is False
        assert "Aborted" in capsys.readouterr().out

    def test_destroy_tty_prompt_eof_aborts(self, manager: Any, capsys: Any) -> None:
        inventory = {
            "repo_count": 1,
            "tag_count": 1,
            "total_bytes": 0,
            "endpoint_refs": 0,
            "job_refs": 0,
        }
        with (
            patch.object(
                manager,
                "_read_images_config",
                return_value={"removal_policy": "destroy", "empty_on_delete": True},
            ),
            patch.object(manager, "_build_image_registry_inventory", return_value=inventory),
            patch("cli.stacks.sys.stdin") as mock_stdin,
            patch("builtins.input", side_effect=EOFError),
        ):
            mock_stdin.isatty.return_value = True
            assert manager._image_registry_destroy_preflight(force=False) is False
        assert "Aborted" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# CloudFormation helpers
# ---------------------------------------------------------------------------


class TestCloudFormationHelpers:
    _STACK_ID = "arn:aws:cloudformation:us-east-2:123456789012:stack/gco-global/stack-uuid"

    @classmethod
    def _stack(cls, status: str) -> dict[str, Any]:
        return {
            "StackName": "gco-global",
            "StackId": cls._STACK_ID,
            "StackStatus": status,
        }

    def test_stack_exists_in_cloudformation_true(self, manager: Any) -> None:
        cfn = MagicMock()
        cfn.describe_stacks.return_value = {"Stacks": [self._stack("CREATE_COMPLETE")]}
        with patch("boto3.client", return_value=cfn):
            assert manager._stack_exists_in_cloudformation("gco-global") is True

    def test_stack_exists_returns_false_on_delete_status(self, manager: Any) -> None:
        cfn = MagicMock()
        cfn.describe_stacks.return_value = {"Stacks": [self._stack("DELETE_COMPLETE")]}
        with patch("boto3.client", return_value=cfn):
            assert manager._stack_exists_in_cloudformation("gco-global") is False

    def test_stack_exists_returns_false_only_for_not_found(self, manager: Any) -> None:
        cfn = MagicMock()
        cfn.describe_stacks.side_effect = ClientError(
            {
                "Error": {
                    "Code": "ValidationError",
                    "Message": "Stack with id missing does not exist",
                }
            },
            "DescribeStacks",
        )
        with patch("boto3.client", return_value=cfn):
            assert manager._stack_exists_in_cloudformation("missing") is False

    def test_stack_exists_propagates_describe_service_error(self, manager: Any) -> None:
        cfn = MagicMock()
        cfn.describe_stacks.side_effect = ClientError(
            {"Error": {"Code": "AccessDenied", "Message": "denied"}},
            "DescribeStacks",
        )
        with (
            patch("boto3.client", return_value=cfn),
            pytest.raises(ClientError),
        ):
            manager._stack_exists_in_cloudformation("gco-global")

    def test_cloudformation_delete_stack_success(self, manager: Any) -> None:
        cfn = MagicMock()
        cfn.describe_stacks.return_value = {"Stacks": [self._stack("CREATE_COMPLETE")]}
        authorize = MagicMock()
        with (
            patch("boto3.client", return_value=cfn),
            patch.object(
                manager,
                "_wait_for_stack_delete_convergence",
                return_value=True,
            ) as wait,
        ):
            assert (
                manager._cloudformation_delete_stack(
                    "gco-global",
                    expected_stack_id=self._STACK_ID,
                    authorize_stack=authorize,
                )
                is True
            )
        cfn.describe_stacks.assert_called_once_with(StackName=self._STACK_ID)
        authorize.assert_called_once_with("gco-global", "us-east-2", self._STACK_ID)
        cfn.delete_stack.assert_called_once_with(StackName=self._STACK_ID)
        wait.assert_called_once_with("gco-global", expected_stack_id=self._STACK_ID)

    def test_cloudformation_delete_stack_rejects_same_name_replacement(self, manager: Any) -> None:
        missing = ClientError(
            {
                "Error": {
                    "Code": "ValidationError",
                    "Message": "Stack does not exist",
                }
            },
            "DescribeStacks",
        )
        replacement = {
            **self._stack("CREATE_COMPLETE"),
            "StackId": self._STACK_ID.replace("stack-uuid", "replacement"),
        }
        cfn = MagicMock()
        cfn.describe_stacks.side_effect = [missing, {"Stacks": [replacement]}]
        with (
            patch("boto3.client", return_value=cfn),
            pytest.raises(RuntimeError, match="same-name replacement"),
        ):
            manager._cloudformation_delete_stack(
                "gco-global",
                expected_stack_id=self._STACK_ID,
            )
        cfn.delete_stack.assert_not_called()

    def test_cloudformation_delete_stack_failure(self, manager: Any) -> None:
        cfn = MagicMock()
        cfn.describe_stacks.return_value = {"Stacks": [self._stack("CREATE_COMPLETE")]}
        cfn.delete_stack.side_effect = RuntimeError("denied")
        with patch("boto3.client", return_value=cfn):
            assert manager._cloudformation_delete_stack("gco-global") is False

    def test_get_destroy_region_falls_back_to_api_gateway(self, manager: Any) -> None:
        with patch.object(manager, "_get_deploy_region", return_value=None):
            assert manager._get_destroy_region("gco-other") == "us-east-2"

    def test_get_destroy_region_returns_resolved(self, manager: Any) -> None:
        with patch.object(manager, "_get_deploy_region", return_value="eu-west-1"):
            assert manager._get_destroy_region("gco-eu-west-1") == "eu-west-1"

    def test_get_destroy_region_handles_exception(self, manager: Any) -> None:
        with patch.object(manager, "_get_deploy_region", side_effect=RuntimeError("nope")):
            assert manager._get_destroy_region("gco-global") == "us-east-2"


# ---------------------------------------------------------------------------
# Analytics toggle helpers
# ---------------------------------------------------------------------------


class TestAnalyticsToggle:
    def test_ensure_analytics_enabled_flips_when_disabled(self, manager: Any) -> None:
        with (
            patch("cli.stacks.get_analytics_config", return_value={"enabled": False}),
            patch("cli.stacks.update_analytics_config") as mock_update,
        ):
            assert manager._ensure_analytics_enabled_for_destroy() is True
            mock_update.assert_called_once_with({"enabled": True})

    def test_ensure_analytics_enabled_no_op_when_already_enabled(self, manager: Any) -> None:
        with (
            patch("cli.stacks.get_analytics_config", return_value={"enabled": True}),
            patch("cli.stacks.update_analytics_config") as mock_update,
        ):
            assert manager._ensure_analytics_enabled_for_destroy() is False
            mock_update.assert_not_called()

    def test_ensure_analytics_enabled_handles_exception(self, manager: Any) -> None:
        with patch("cli.stacks.get_analytics_config", side_effect=RuntimeError("missing")):
            assert manager._ensure_analytics_enabled_for_destroy() is False

    def test_restore_analytics_disabled(self, manager: Any) -> None:
        with patch("cli.stacks.update_analytics_config") as mock_update:
            manager._restore_analytics_disabled()
            mock_update.assert_called_once_with({"enabled": False})

    def test_restore_analytics_disabled_swallows_errors(self, manager: Any) -> None:
        with patch("cli.stacks.update_analytics_config", side_effect=RuntimeError("denied")):
            # Must not raise.
            manager._restore_analytics_disabled()

    def test_public_destroy_restores_exact_bytes_after_baseexception(
        self,
        manager: Any,
        tmp_path: Any,
    ) -> None:
        path = tmp_path / "cdk.json"
        original = b'{\n  "context": {"analytics": {"enabled": false}}\n}\n'
        path.write_bytes(original)
        path.chmod(0o640)

        def interrupting_destroy(**_kwargs: Any) -> bool:
            path.write_text('{"context":{"analytics":{"enabled":true}}}')
            raise KeyboardInterrupt

        with (
            patch("cli.stacks._find_cdk_json", return_value=path),
            patch.object(manager, "_destroy", side_effect=interrupting_destroy),
            pytest.raises(KeyboardInterrupt),
        ):
            manager.destroy("gco-analytics", force=True)

        assert path.read_bytes() == original
        assert stat.S_IMODE(path.stat().st_mode) == 0o640


# ---------------------------------------------------------------------------
# _api_gateway_imports_from_analytics
# ---------------------------------------------------------------------------


def _make_paginator(pages: list[dict[str, Any]]) -> Any:
    paginator = MagicMock()
    paginator.paginate.return_value = iter(pages)
    return paginator


def _make_paginator_callable(pages: list[dict[str, Any]]) -> Any:
    """Paginator whose paginate(...) yields fresh iterators each call."""
    paginator = MagicMock()

    def _paginate(*args: Any, **kwargs: Any) -> Any:
        return iter(pages)

    paginator.paginate.side_effect = _paginate
    return paginator


class TestApiGatewayImportsFromAnalytics:
    def test_returns_false_when_no_region(self, manager: Any) -> None:
        with patch.object(manager, "_get_deploy_region", return_value=None):
            assert manager._api_gateway_imports_from_analytics() is False

    def test_returns_false_when_no_analytics_exports(self, manager: Any) -> None:
        cfn = MagicMock()
        cfn.get_paginator.return_value = _make_paginator(
            [
                {
                    "Exports": [
                        {
                            "Name": "other-export",
                            "ExportingStackId": "arn:aws:cloudformation:us-east-2:123:stack/other/abc",
                        }
                    ]
                }
            ]
        )
        with (
            patch.object(manager, "_get_deploy_region", return_value="us-east-2"),
            patch("boto3.client", return_value=cfn),
        ):
            assert manager._api_gateway_imports_from_analytics() is False

    def test_returns_true_when_api_gateway_imports(self, manager: Any) -> None:
        cfn = MagicMock()

        def get_paginator(op: str) -> Any:
            if op == "list_exports":
                return _make_paginator_callable(
                    [
                        {
                            "Exports": [
                                {
                                    "Name": "analytics-pool-arn",
                                    "ExportingStackId": (
                                        "arn:aws:cloudformation:us-east-2:123"
                                        ":stack/gco-analytics/abc"
                                    ),
                                }
                            ]
                        }
                    ]
                )
            return _make_paginator_callable([{"Imports": ["gco-api-gateway"]}])

        cfn.get_paginator.side_effect = get_paginator
        with (
            patch.object(manager, "_get_deploy_region", return_value="us-east-2"),
            patch("boto3.client", return_value=cfn),
        ):
            assert manager._api_gateway_imports_from_analytics() is True

    def test_returns_false_on_unexpected_error(self, manager: Any) -> None:
        cfn = MagicMock()
        cfn.get_paginator.side_effect = RuntimeError("denied")
        with (
            patch.object(manager, "_get_deploy_region", return_value="us-east-2"),
            patch("boto3.client", return_value=cfn),
        ):
            # The bare-except branch returns True on outer-error to be
            # safe (force the redeploy attempt).
            assert manager._api_gateway_imports_from_analytics() is True

    def test_swallows_list_imports_failure(self, manager: Any) -> None:
        cfn = MagicMock()

        def get_paginator(op: str) -> Any:
            if op == "list_exports":
                return _make_paginator_callable(
                    [
                        {
                            "Exports": [
                                {
                                    "Name": "analytics-pool-arn",
                                    "ExportingStackId": (
                                        "arn:aws:cloudformation:us-east-2:123"
                                        ":stack/gco-analytics/abc"
                                    ),
                                }
                            ]
                        }
                    ]
                )
            failing = MagicMock()
            failing.paginate.side_effect = RuntimeError("no consumers")
            return failing

        cfn.get_paginator.side_effect = get_paginator
        with (
            patch.object(manager, "_get_deploy_region", return_value="us-east-2"),
            patch("boto3.client", return_value=cfn),
        ):
            assert manager._api_gateway_imports_from_analytics() is False


# ---------------------------------------------------------------------------
# _cleanup_backup_vault
# ---------------------------------------------------------------------------


class TestCleanupBackupVault:
    _STACK_ID = "arn:aws:cloudformation:us-east-2:123456789012:stack/gco-global/stack-uuid"

    @classmethod
    def _clients(
        cls,
        *,
        recovery_delete_error: Exception | None = None,
        resources: list[dict[str, Any]] | None = None,
    ) -> tuple[Any, Any, Any]:
        vault_arn = "arn:aws:backup:us-east-2:123456789012:backup-vault:GcoBackupVault"
        cloudformation = MagicMock()
        cloudformation.describe_stacks.return_value = {
            "Stacks": [
                {
                    "StackName": "gco-global",
                    "StackId": cls._STACK_ID,
                    "StackStatus": "CREATE_COMPLETE",
                }
            ]
        }
        vault_resources = resources
        if vault_resources is None:
            vault_resources = [
                {
                    "LogicalResourceId": "BackupVault",
                    "ResourceType": "AWS::Backup::BackupVault",
                    "PhysicalResourceId": vault_arn,
                }
            ]
        cloudformation.get_paginator.return_value = _make_paginator(
            [{"StackResourceSummaries": vault_resources}]
        )
        backup = MagicMock()
        backup.describe_backup_vault.return_value = {"BackupVaultArn": vault_arn}
        backup.get_paginator.return_value = _make_paginator(
            [
                {
                    "RecoveryPoints": [
                        {"RecoveryPointArn": "arn:aws:backup:rp1"},
                        {"RecoveryPointArn": "arn:aws:backup:rp2"},
                    ]
                }
            ]
        )
        if recovery_delete_error is not None:
            backup.delete_recovery_point.side_effect = recovery_delete_error

        def client(service: str, **_kwargs: Any) -> Any:
            return cloudformation if service == "cloudformation" else backup

        return cloudformation, backup, client

    def test_uses_exact_stack_resource_and_deletes_recovery_points(
        self, manager: Any, capsys: Any
    ) -> None:
        cloudformation, backup, client = self._clients()
        with patch("boto3.client", side_effect=client):
            manager._cleanup_backup_vault()

        cloudformation.describe_stacks.assert_called_once_with(StackName="gco-global")
        cloudformation.get_paginator.assert_called_once_with("list_stack_resources")
        cloudformation.get_paginator.return_value.paginate.assert_called_once_with(
            StackName=self._STACK_ID
        )
        backup.describe_backup_vault.assert_called_once_with(BackupVaultName="GcoBackupVault")
        backup.get_paginator.assert_called_once_with("list_recovery_points_by_backup_vault")
        backup.get_paginator.return_value.paginate.assert_called_once_with(
            BackupVaultName="GcoBackupVault"
        )
        assert backup.delete_recovery_point.call_count == 2
        assert "Cleaned up 2 backup recovery points" in capsys.readouterr().out

    def test_missing_exact_stack_resource_short_circuits(self, manager: Any) -> None:
        cloudformation, backup, client = self._clients(resources=[])
        with patch("boto3.client", side_effect=client):
            result = manager._cleanup_backup_vault()
        assert result["status"] == "vault-resource-absent"
        backup.describe_backup_vault.assert_not_called()

    def test_swallows_top_level_exceptions(self, manager: Any, capsys: Any) -> None:
        cloudformation = MagicMock()
        cloudformation.describe_stacks.side_effect = RuntimeError("denied")
        with patch("boto3.client", return_value=cloudformation):
            manager._cleanup_backup_vault()
        assert "Backup vault cleanup failed" in capsys.readouterr().out

    def test_recovery_point_delete_failure_logged(self, manager: Any) -> None:
        _cloudformation, backup, client = self._clients(
            recovery_delete_error=RuntimeError("denied")
        )
        with patch("boto3.client", side_effect=client):
            manager._cleanup_backup_vault()
        assert backup.delete_recovery_point.call_count == 2


# ---------------------------------------------------------------------------
# EKS security group cleanup + watchdog
# ---------------------------------------------------------------------------


class TestEksSecurityGroupCleanup:
    def test_no_sgs_no_op(self, manager: Any) -> None:
        ec2 = MagicMock()
        ec2.describe_security_groups.return_value = {"SecurityGroups": []}
        with patch("boto3.client", return_value=ec2):
            manager._cleanup_eks_security_groups("gco-us-east-1")
        ec2.delete_security_group.assert_not_called()

    def test_waits_for_aws_when_eks_interfaces_remain(self, manager: Any) -> None:
        ec2 = MagicMock()
        ec2.describe_security_groups.return_value = {
            "SecurityGroups": [
                {"GroupId": "sg-123", "GroupName": "eks-cluster-sg-gco-us-east-1-abc"}
            ]
        }
        ec2.describe_network_interfaces.return_value = {
            "NetworkInterfaces": [
                {
                    "NetworkInterfaceId": "eni-1",
                    "RequesterManaged": True,
                    "Attachment": {"AttachmentId": "eni-attach-1"},
                }
            ]
        }
        with patch("boto3.client", return_value=ec2):
            manager._cleanup_eks_security_groups("gco-us-east-1")
        ec2.detach_network_interface.assert_not_called()
        ec2.delete_network_interface.assert_not_called()
        ec2.delete_security_group.assert_not_called()

    def test_deletes_security_group_only_after_interfaces_are_absent(
        self, manager: Any, capsys: Any
    ) -> None:
        ec2 = MagicMock()
        ec2.describe_security_groups.return_value = {
            "SecurityGroups": [{"GroupId": "sg-x", "GroupName": "eks-cluster-sg-x"}]
        }
        ec2.describe_network_interfaces.return_value = {"NetworkInterfaces": []}
        with patch("boto3.client", return_value=ec2):
            manager._cleanup_eks_security_groups("gco-us-east-1")
        ec2.delete_security_group.assert_called_once_with(GroupId="sg-x")
        assert "Cleaned up empty EKS security group" in capsys.readouterr().out

    def test_sg_delete_failure_logged(self, manager: Any) -> None:
        ec2 = MagicMock()
        ec2.describe_security_groups.return_value = {
            "SecurityGroups": [{"GroupId": "sg-x", "GroupName": "eks-cluster-sg-x"}]
        }
        ec2.describe_network_interfaces.return_value = {"NetworkInterfaces": []}
        ec2.delete_security_group.side_effect = RuntimeError("dependency")
        with patch("boto3.client", return_value=ec2):
            manager._cleanup_eks_security_groups("gco-us-east-1")  # no raise

    def test_top_level_failure_logged(self, manager: Any) -> None:
        ec2 = MagicMock()
        ec2.describe_security_groups.side_effect = RuntimeError("denied")
        with patch("boto3.client", return_value=ec2):
            manager._cleanup_eks_security_groups("gco-us-east-1")  # no raise

    def test_cleanup_eks_security_groups_skips_global_stacks(self, manager: Any) -> None:
        with (
            patch.object(
                manager,
                "list_stacks",
                return_value=["gco-global", "gco-api-gateway", "gco-monitoring", "gco-us-east-1"],
            ),
            patch.object(manager, "_cleanup_eks_security_groups") as mock_clean,
        ):
            manager.cleanup_eks_security_groups()
        # Only the regional stack is cleaned.
        called_stacks = [c.args[0] for c in mock_clean.call_args_list]
        assert called_stacks == ["gco-us-east-1"]


class TestEksSgWatchdog:
    def test_watchdog_runs_cleanup_until_stop_event(self, manager: Any) -> None:
        stop = threading.Event()
        # Trip the stop event after the first sweep so the thread exits
        # promptly. The cleanup helper is mocked to record call count.
        calls = []

        def fake_cleanup(name: str) -> None:
            calls.append(name)
            stop.set()

        with patch.object(manager, "_cleanup_eks_security_groups", side_effect=fake_cleanup):
            thread = manager._start_eks_sg_watchdog("gco-us-east-1", stop)
            thread.join(timeout=5)
        assert calls and calls[0] == "gco-us-east-1"
        assert thread.is_alive() is False

    def test_watchdog_swallows_cleanup_exception(self, manager: Any) -> None:
        stop = threading.Event()

        def fake_cleanup(name: str) -> None:
            stop.set()
            raise RuntimeError("transient")

        with patch.object(manager, "_cleanup_eks_security_groups", side_effect=fake_cleanup):
            thread = manager._start_eks_sg_watchdog("gco-us-east-1", stop)
            thread.join(timeout=5)
        assert thread.is_alive() is False


# ---------------------------------------------------------------------------
# CLI subcommands: ``gco stacks fsx/valkey/aurora`` enable / disable / status
# ---------------------------------------------------------------------------
#
# The underlying ``update_*_config`` helpers are exercised by
# ``tests/test_feature_toggles.py``. These tests target the click-handler
# bodies in ``cli/commands/stacks_cmd.py``: validation, the confirmation
# branch, the success branch, and the catch-all ``except Exception`` path
# that prints an error and exits non-zero.


class TestFsxCliErrorPaths:
    """``gco stacks fsx`` enable/disable/status — error and edge branches."""

    def test_status_propagates_underlying_failure(self) -> None:
        from click.testing import CliRunner

        from cli.main import cli

        with patch("cli.stacks.get_fsx_config", side_effect=RuntimeError("boom")):
            result = CliRunner().invoke(cli, ["stacks", "fsx", "status"])
        assert result.exit_code == 1
        assert "Failed to get FSx config: boom" in result.output

    def test_status_with_region_prints_region_label(self) -> None:
        from click.testing import CliRunner

        from cli.main import cli

        with patch(
            "cli.stacks.get_fsx_config",
            return_value={"enabled": True, "storage_capacity_gib": 1200},
        ) as mock_get:
            result = CliRunner().invoke(cli, ["stacks", "fsx", "status", "-r", "us-east-1"])
        assert result.exit_code == 0
        assert "us-east-1" in result.output
        mock_get.assert_called_once_with("us-east-1")

    def test_enable_update_raises(self) -> None:
        from click.testing import CliRunner

        from cli.main import cli

        with patch("cli.stacks.update_fsx_config", side_effect=RuntimeError("disk full")):
            result = CliRunner().invoke(cli, ["stacks", "fsx", "enable", "-y"])
        assert result.exit_code == 1
        assert "Failed to enable FSx: disk full" in result.output

    def test_enable_per_region_prints_region_specific_followup(self) -> None:
        from click.testing import CliRunner

        from cli.main import cli

        with patch("cli.stacks.update_fsx_config") as mock_update:
            result = CliRunner().invoke(cli, ["stacks", "fsx", "enable", "-y", "-r", "us-west-2"])
        assert result.exit_code == 0
        # Per-region invocations show the regional deploy hint.
        assert "gco-us-west-2" in result.output
        mock_update.assert_called_once()

    def test_enable_with_export_path_passes_through(self) -> None:
        from click.testing import CliRunner

        from cli.main import cli

        with patch("cli.stacks.update_fsx_config") as mock_update:
            result = CliRunner().invoke(
                cli,
                [
                    "stacks",
                    "fsx",
                    "enable",
                    "-y",
                    "--export-path",
                    "s3://bucket/out",
                ],
            )
        assert result.exit_code == 0
        kwargs = mock_update.call_args.args[0]
        assert kwargs["export_path"] == "s3://bucket/out"
        # No import_path given, auto_import_policy must remain None.
        assert kwargs["auto_import_policy"] is None

    def test_disable_update_raises(self) -> None:
        from click.testing import CliRunner

        from cli.main import cli

        with patch("cli.stacks.update_fsx_config", side_effect=RuntimeError("locked")):
            result = CliRunner().invoke(cli, ["stacks", "fsx", "disable", "-y"])
        assert result.exit_code == 1
        assert "Failed to disable FSx: locked" in result.output

    def test_disable_per_region_prints_region_specific_followup(self) -> None:
        from click.testing import CliRunner

        from cli.main import cli

        with patch("cli.stacks.update_fsx_config") as mock_update:
            result = CliRunner().invoke(cli, ["stacks", "fsx", "disable", "-y", "-r", "eu-west-1"])
        assert result.exit_code == 0
        assert "gco-eu-west-1" in result.output
        mock_update.assert_called_once_with({"enabled": False}, "eu-west-1")


class TestValkeyCli:
    """``gco stacks valkey`` enable/disable/status — full surface."""

    def test_status_happy(self) -> None:
        from click.testing import CliRunner

        from cli.main import cli

        with patch(
            "cli.stacks.get_valkey_config",
            return_value={"enabled": False},
        ):
            result = CliRunner().invoke(cli, ["stacks", "valkey", "status"])
        assert result.exit_code == 0
        assert "Valkey config" in result.output

    def test_status_failure(self) -> None:
        from click.testing import CliRunner

        from cli.main import cli

        with patch("cli.stacks.get_valkey_config", side_effect=RuntimeError("nope")):
            result = CliRunner().invoke(cli, ["stacks", "valkey", "status"])
        assert result.exit_code == 1
        assert "Failed to get Valkey config: nope" in result.output

    def test_enable_happy(self) -> None:
        from click.testing import CliRunner

        from cli.main import cli

        with patch("cli.stacks.update_valkey_config") as mock_update:
            result = CliRunner().invoke(
                cli,
                [
                    "stacks",
                    "valkey",
                    "enable",
                    "-y",
                    "--max-storage",
                    "10",
                    "--max-ecpu",
                    "8000",
                    "--snapshot-retention",
                    "3",
                ],
            )
        assert result.exit_code == 0
        kwargs = mock_update.call_args.args[0]
        assert kwargs["enabled"] is True
        assert kwargs["max_data_storage_gb"] == 10
        assert kwargs["max_ecpu_per_second"] == 8000
        assert kwargs["snapshot_retention_limit"] == 3

    def test_enable_update_raises(self) -> None:
        from click.testing import CliRunner

        from cli.main import cli

        with patch("cli.stacks.update_valkey_config", side_effect=RuntimeError("kaboom")):
            result = CliRunner().invoke(cli, ["stacks", "valkey", "enable", "-y"])
        assert result.exit_code == 1
        assert "Failed to enable Valkey: kaboom" in result.output

    def test_disable_happy(self) -> None:
        from click.testing import CliRunner

        from cli.main import cli

        with patch("cli.stacks.update_valkey_config") as mock_update:
            result = CliRunner().invoke(cli, ["stacks", "valkey", "disable", "-y"])
        assert result.exit_code == 0
        mock_update.assert_called_once_with({"enabled": False})

    def test_disable_update_raises(self) -> None:
        from click.testing import CliRunner

        from cli.main import cli

        with patch("cli.stacks.update_valkey_config", side_effect=RuntimeError("locked")):
            result = CliRunner().invoke(cli, ["stacks", "valkey", "disable", "-y"])
        assert result.exit_code == 1
        assert "Failed to disable Valkey: locked" in result.output


class TestAuroraCli:
    """``gco stacks aurora`` enable/disable/status — full surface."""

    def test_status_happy(self) -> None:
        from click.testing import CliRunner

        from cli.main import cli

        with patch(
            "cli.stacks.get_aurora_config",
            return_value={"enabled": False, "min_acu": 0, "max_acu": 16},
        ):
            result = CliRunner().invoke(cli, ["stacks", "aurora", "status"])
        assert result.exit_code == 0
        assert "Aurora pgvector config" in result.output

    def test_status_failure(self) -> None:
        from click.testing import CliRunner

        from cli.main import cli

        with patch("cli.stacks.get_aurora_config", side_effect=RuntimeError("denied")):
            result = CliRunner().invoke(cli, ["stacks", "aurora", "status"])
        assert result.exit_code == 1
        assert "Failed to get Aurora config: denied" in result.output

    def test_enable_rejects_negative_min_acu(self) -> None:
        from click.testing import CliRunner

        from cli.main import cli

        result = CliRunner().invoke(cli, ["stacks", "aurora", "enable", "-y", "--min-acu", "-1"])
        assert result.exit_code == 1
        assert "Minimum ACU must be >= 0" in result.output

    def test_enable_rejects_zero_max_acu(self) -> None:
        from click.testing import CliRunner

        from cli.main import cli

        result = CliRunner().invoke(cli, ["stacks", "aurora", "enable", "-y", "--max-acu", "0"])
        assert result.exit_code == 1
        assert "Maximum ACU must be >= 1" in result.output

    def test_enable_rejects_max_below_min(self) -> None:
        from click.testing import CliRunner

        from cli.main import cli

        result = CliRunner().invoke(
            cli,
            [
                "stacks",
                "aurora",
                "enable",
                "-y",
                "--min-acu",
                "10",
                "--max-acu",
                "5",
            ],
        )
        assert result.exit_code == 1
        assert "Maximum ACU must be >= minimum ACU" in result.output

    def test_enable_happy_with_deletion_protection(self) -> None:
        from click.testing import CliRunner

        from cli.main import cli

        with patch("cli.stacks.update_aurora_config") as mock_update:
            result = CliRunner().invoke(
                cli,
                [
                    "stacks",
                    "aurora",
                    "enable",
                    "-y",
                    "--min-acu",
                    "2",
                    "--max-acu",
                    "32",
                    "--backup-retention",
                    "14",
                    "--deletion-protection",
                ],
            )
        assert result.exit_code == 0
        kwargs = mock_update.call_args.args[0]
        assert kwargs["enabled"] is True
        assert kwargs["min_acu"] == 2
        assert kwargs["max_acu"] == 32
        assert kwargs["backup_retention_days"] == 14
        assert kwargs["deletion_protection"] is True

    def test_enable_update_raises(self) -> None:
        from click.testing import CliRunner

        from cli.main import cli

        with patch("cli.stacks.update_aurora_config", side_effect=RuntimeError("limit")):
            result = CliRunner().invoke(cli, ["stacks", "aurora", "enable", "-y"])
        assert result.exit_code == 1
        assert "Failed to enable Aurora: limit" in result.output

    def test_disable_happy(self) -> None:
        from click.testing import CliRunner

        from cli.main import cli

        with patch("cli.stacks.update_aurora_config") as mock_update:
            result = CliRunner().invoke(cli, ["stacks", "aurora", "disable", "-y"])
        assert result.exit_code == 0
        mock_update.assert_called_once_with({"enabled": False})

    def test_disable_update_raises(self) -> None:
        from click.testing import CliRunner

        from cli.main import cli

        with patch("cli.stacks.update_aurora_config", side_effect=RuntimeError("snapshot")):
            result = CliRunner().invoke(cli, ["stacks", "aurora", "disable", "-y"])
        assert result.exit_code == 1
        assert "Failed to disable Aurora: snapshot" in result.output


# ---------------------------------------------------------------------------
# Strict live-validation deployment and teardown authority
# ---------------------------------------------------------------------------


class TestStrictPreparedChangeSets:
    _REGION = "us-east-2"
    _ACCOUNT = "123456789012"
    _STACK_NAME = "gco-global"
    _STACK_ID = "arn:aws:cloudformation:us-east-2:123456789012:stack/gco-global/stack-uuid"
    _CHANGE_SET_NAME = "run-123-change-set"
    _CHANGE_SET_ID = (
        "arn:aws:cloudformation:us-east-2:123456789012:changeSet/run-123-change-set/change-set-uuid"
    )
    _RUN_TAGS = {"GcoLiveValidationRun": "run-123"}

    def _change_set(
        self,
        *,
        status: str = "CREATE_COMPLETE",
        execution_status: str = "AVAILABLE",
        status_reason: str = "",
        stack_id: str | None = None,
        change_set_id: str | None = None,
        tags: list[dict[str, str]] | None = None,
    ) -> dict[str, Any]:
        return {
            "ChangeSetName": self._CHANGE_SET_NAME,
            "ChangeSetId": change_set_id or self._CHANGE_SET_ID,
            "StackId": stack_id or self._STACK_ID,
            "Status": status,
            "ExecutionStatus": execution_status,
            "StatusReason": status_reason,
            "Tags": (
                [{"Key": key, "Value": value} for key, value in self._RUN_TAGS.items()]
                if tags is None
                else tags
            ),
        }

    def _prepared_authority(self, change_set_type: str) -> dict[str, dict[str, str]]:
        return {
            self._CHANGE_SET_ID: {
                "change_set_id": self._CHANGE_SET_ID,
                "stack_id": self._STACK_ID,
                "change_set_type": change_set_type,
            }
        }

    def test_update_is_checkpointed_before_exact_execution(self, manager: Any) -> None:
        cfn = MagicMock()
        cfn.describe_change_set.return_value = self._change_set()
        authorize = MagicMock()
        events: list[str] = []

        def prepared(*_args: Any) -> None:
            events.append("checkpoint")

        cfn.execute_change_set.side_effect = lambda **_kwargs: events.append("execute")
        with (
            patch.object(manager, "_get_deploy_region", return_value=self._REGION),
            patch("boto3.client", return_value=cfn),
            patch.object(
                manager,
                "_wait_for_stack_settle",
                return_value="UPDATE_COMPLETE",
            ) as wait,
        ):
            result = manager._execute_prepared_change_set(
                stack_name=self._STACK_NAME,
                change_set_name=self._CHANGE_SET_NAME,
                expected_stack_id=self._STACK_ID,
                expected_tags=self._RUN_TAGS,
                prepared_change_sets={},
                preparation_succeeded=True,
                authorize_stack=authorize,
                on_change_set_prepared=prepared,
                allow_noop=False,
                timeout=42,
            )

        assert result is True
        assert events == ["checkpoint", "execute"]
        authorize.assert_called_once_with(self._STACK_NAME, self._REGION, self._STACK_ID)
        cfn.execute_change_set.assert_called_once_with(ChangeSetName=self._CHANGE_SET_ID)
        wait.assert_called_once_with(
            self._STACK_NAME,
            timeout=42,
            stack_identifier=self._STACK_ID,
        )

    def test_create_accepts_only_related_cloudformation_arns(self, manager: Any) -> None:
        cfn = MagicMock()
        cfn.describe_change_set.return_value = self._change_set()
        review_target = (
            self._REGION,
            cfn,
            {"StackId": self._STACK_ID, "StackStatus": "REVIEW_IN_PROGRESS"},
        )
        prepared = MagicMock()
        authorize = MagicMock()
        with (
            patch.object(manager, "_get_deploy_region", return_value=self._REGION),
            patch("boto3.client", return_value=cfn),
            patch.object(manager, "_describe_stack_target", return_value=review_target) as describe,
            patch.object(manager, "_wait_for_stack_settle", return_value="CREATE_COMPLETE"),
        ):
            result = manager._execute_prepared_change_set(
                stack_name=self._STACK_NAME,
                change_set_name=self._CHANGE_SET_NAME,
                expected_stack_id=None,
                expected_tags=self._RUN_TAGS,
                prepared_change_sets={},
                preparation_succeeded=True,
                authorize_stack=authorize,
                on_change_set_prepared=prepared,
                allow_noop=False,
                timeout=42,
            )

        assert result is True
        authorize.assert_not_called()
        describe.assert_called_once_with(
            self._STACK_NAME,
            expected_stack_id=self._STACK_ID,
            require_expected_identity=True,
        )
        prepared.assert_called_once_with(
            self._STACK_NAME,
            self._REGION,
            self._STACK_ID,
            self._CHANGE_SET_ID,
            "CREATE",
        )

    def test_fresh_create_rejects_a_same_name_stack_race(self, manager: Any) -> None:
        cfn = MagicMock()
        cfn.describe_change_set.return_value = self._change_set()
        raced_target = (
            self._REGION,
            cfn,
            {"StackId": self._STACK_ID, "StackStatus": "CREATE_COMPLETE"},
        )
        prepared = MagicMock()
        with (
            patch.object(manager, "_get_deploy_region", return_value=self._REGION),
            patch("boto3.client", return_value=cfn),
            patch.object(manager, "_describe_stack_target", return_value=raced_target),
            pytest.raises(RuntimeError, match="no exact review stack"),
        ):
            manager._execute_prepared_change_set(
                stack_name=self._STACK_NAME,
                change_set_name=self._CHANGE_SET_NAME,
                expected_stack_id=None,
                expected_tags=self._RUN_TAGS,
                prepared_change_sets={},
                preparation_succeeded=True,
                authorize_stack=MagicMock(),
                on_change_set_prepared=prepared,
                allow_noop=False,
                timeout=42,
            )

        prepared.assert_not_called()
        cfn.execute_change_set.assert_not_called()

    @pytest.mark.parametrize(
        ("stack_id", "change_set_id", "message"),
        (
            ("not-an-arn", _CHANGE_SET_ID, "stack identity"),
            (
                _STACK_ID,
                (
                    "arn:aws:cloudformation:us-west-2:123456789012:"
                    "changeSet/run-123-change-set/change-set-uuid"
                ),
                "change-set identity",
            ),
            (
                _STACK_ID,
                (
                    "arn:aws:cloudformation:us-east-2:999999999999:"
                    "changeSet/run-123-change-set/change-set-uuid"
                ),
                "different AWS authorities",
            ),
        ),
    )
    def test_rejects_unrelated_prepared_identities(
        self,
        manager: Any,
        stack_id: str,
        change_set_id: str,
        message: str,
    ) -> None:
        cfn = MagicMock()
        cfn.describe_change_set.return_value = self._change_set(
            stack_id=stack_id,
            change_set_id=change_set_id,
        )
        prepared = MagicMock()
        with (
            patch.object(manager, "_get_deploy_region", return_value=self._REGION),
            patch("boto3.client", return_value=cfn),
            pytest.raises(RuntimeError, match=message),
        ):
            manager._execute_prepared_change_set(
                stack_name=self._STACK_NAME,
                change_set_name=self._CHANGE_SET_NAME,
                expected_stack_id=self._STACK_ID if stack_id == self._STACK_ID else None,
                expected_tags=self._RUN_TAGS,
                prepared_change_sets={},
                preparation_succeeded=True,
                authorize_stack=MagicMock(),
                on_change_set_prepared=prepared,
                allow_noop=False,
                timeout=42,
            )

        prepared.assert_not_called()
        cfn.execute_change_set.assert_not_called()

    def test_failed_empty_update_requires_tags_and_exact_healthy_stack(
        self,
        manager: Any,
    ) -> None:
        cfn = MagicMock()
        cfn.describe_change_set.return_value = self._change_set(
            status="FAILED",
            execution_status="UNAVAILABLE",
            status_reason="The submitted information didn't contain changes.",
        )
        target = (
            self._REGION,
            cfn,
            {"StackId": self._STACK_ID, "StackStatus": "UPDATE_COMPLETE"},
        )
        persisted: dict[str, dict[str, str]] = {}

        def persist(
            _stack_name: str,
            _region: str,
            stack_id: str,
            change_set_id: str,
            change_set_type: str,
        ) -> None:
            persisted[change_set_id] = {
                "change_set_id": change_set_id,
                "stack_id": stack_id,
                "change_set_type": change_set_type,
            }

        authorize = MagicMock()
        prepared = MagicMock(side_effect=persist)
        with (
            patch.object(manager, "_get_deploy_region", return_value=self._REGION),
            patch("boto3.client", return_value=cfn),
            patch.object(manager, "_describe_stack_target", return_value=target),
        ):
            result = manager._execute_prepared_change_set(
                stack_name=self._STACK_NAME,
                change_set_name=self._CHANGE_SET_NAME,
                expected_stack_id=self._STACK_ID,
                expected_tags=self._RUN_TAGS,
                prepared_change_sets={},
                preparation_succeeded=True,
                authorize_stack=authorize,
                on_change_set_prepared=prepared,
                allow_noop=True,
                timeout=42,
            )

        assert result is True
        authorize.assert_called_once_with(self._STACK_NAME, self._REGION, self._STACK_ID)
        prepared.assert_called_once_with(
            self._STACK_NAME,
            self._REGION,
            self._STACK_ID,
            self._CHANGE_SET_ID,
            "UPDATE",
        )
        cfn.execute_change_set.assert_not_called()

        prepared.reset_mock()
        authorize.reset_mock()
        with (
            patch.object(manager, "_get_deploy_region", return_value=self._REGION),
            patch("boto3.client", return_value=cfn),
            patch.object(manager, "_describe_stack_target", return_value=target),
        ):
            replayed = manager._execute_prepared_change_set(
                stack_name=self._STACK_NAME,
                change_set_name=self._CHANGE_SET_NAME,
                expected_stack_id=self._STACK_ID,
                expected_tags=self._RUN_TAGS,
                prepared_change_sets=persisted,
                preparation_succeeded=False,
                authorize_stack=authorize,
                on_change_set_prepared=prepared,
                allow_noop=False,
                timeout=42,
            )

        assert replayed is True
        authorize.assert_called_once_with(self._STACK_NAME, self._REGION, self._STACK_ID)
        prepared.assert_called_once_with(
            self._STACK_NAME,
            self._REGION,
            self._STACK_ID,
            self._CHANGE_SET_ID,
            "UPDATE",
        )
        cfn.execute_change_set.assert_not_called()

        prepared.reset_mock()
        cfn.describe_change_set.return_value = self._change_set(
            status="FAILED",
            execution_status="UNAVAILABLE",
            status_reason="No updates are to be performed.",
            tags=[],
        )
        with (
            patch.object(manager, "_get_deploy_region", return_value=self._REGION),
            patch("boto3.client", return_value=cfn),
            pytest.raises(RuntimeError, match="omitted required tag"),
        ):
            manager._execute_prepared_change_set(
                stack_name=self._STACK_NAME,
                change_set_name=self._CHANGE_SET_NAME,
                expected_stack_id=self._STACK_ID,
                expected_tags=self._RUN_TAGS,
                prepared_change_sets={},
                preparation_succeeded=True,
                authorize_stack=authorize,
                on_change_set_prepared=prepared,
                allow_noop=True,
                timeout=42,
            )

    @pytest.mark.parametrize("preparation_failure", ("nonzero", "timeout"))
    def test_deploy_replays_persisted_failed_empty_after_preparation_failure(
        self,
        manager: Any,
        monkeypatch: pytest.MonkeyPatch,
        preparation_failure: str,
    ) -> None:
        cfn = MagicMock()
        cfn.describe_change_set.return_value = self._change_set(
            status="FAILED",
            execution_status="UNAVAILABLE",
            status_reason="The submitted information didn't contain changes.",
        )
        target = (
            self._REGION,
            cfn,
            {"StackId": self._STACK_ID, "StackStatus": "UPDATE_COMPLETE"},
        )
        run_cdk = MagicMock()
        if preparation_failure == "nonzero":
            run_cdk.return_value = subprocess.CompletedProcess(["cdk"], 1)
        else:
            run_cdk.side_effect = subprocess.TimeoutExpired(["cdk"], timeout=42)
        monkeypatch.setenv("GCO_CDK_DEPLOY_TIMEOUT_SECONDS", "42")

        authorize = MagicMock()
        prepared = MagicMock()
        bootstrap_id = (
            "arn:aws:cloudformation:us-east-2:123456789012:stack/CDKToolkit/bootstrap-uuid"
        )
        with (
            patch.object(manager, "_sync_lambda_sources"),
            patch.object(manager, "_ensure_lambda_build"),
            patch.object(manager, "_get_deploy_region", return_value=self._REGION),
            patch.object(manager, "_validate_bootstrap_stack"),
            patch.object(
                manager,
                "_strict_change_set_name",
                return_value=self._CHANGE_SET_NAME,
            ),
            patch.object(manager, "_describe_stack_target", return_value=target),
            patch.object(manager, "_mirror_images_if_enabled"),
            patch.object(manager, "_run_cdk", new=run_cdk),
            patch.object(manager, "_diagnose_deploy_failure") as diagnose,
            patch("cli.stacks._detect_container_runtime", return_value="docker"),
            patch("boto3.client", return_value=cfn),
        ):
            result = manager.deploy(
                stack_name=self._STACK_NAME,
                require_approval=False,
                tags=self._RUN_TAGS,
                allow_bootstrap=False,
                bootstrap_stacks={self._REGION: {"stack_id": bootstrap_id}},
                expected_stack_ids={self._STACK_NAME: self._STACK_ID},
                prepared_change_sets={self._STACK_NAME: self._prepared_authority("UPDATE")},
                authorize_stack=authorize,
                strict_deployment_token="run-123",
                on_change_set_prepared=prepared,
            )

        assert result is True
        assert cfn.describe_change_set.call_count == 2
        authorize.assert_called_once_with(self._STACK_NAME, self._REGION, self._STACK_ID)
        prepared.assert_called_once_with(
            self._STACK_NAME,
            self._REGION,
            self._STACK_ID,
            self._CHANGE_SET_ID,
            "UPDATE",
        )
        cfn.execute_change_set.assert_not_called()
        diagnose.assert_not_called()

    def test_cancellation_after_checkpoint_never_executes_change_set(
        self,
        manager: Any,
    ) -> None:
        cfn = MagicMock()
        cfn.describe_change_set.return_value = self._change_set()

        def prepared(*_args: Any) -> None:
            manager._cdk_cancel_event.set()

        with (
            patch.object(manager, "_get_deploy_region", return_value=self._REGION),
            patch("boto3.client", return_value=cfn),
            pytest.raises(RuntimeError, match="checkpointed but execution was cancelled"),
        ):
            manager._execute_prepared_change_set(
                stack_name=self._STACK_NAME,
                change_set_name=self._CHANGE_SET_NAME,
                expected_stack_id=self._STACK_ID,
                expected_tags=self._RUN_TAGS,
                prepared_change_sets={},
                preparation_succeeded=True,
                authorize_stack=MagicMock(),
                on_change_set_prepared=prepared,
                allow_noop=False,
                timeout=42,
            )

        cfn.execute_change_set.assert_not_called()

    def test_missing_change_set_reconciles_only_the_exact_healthy_noop(
        self,
        manager: Any,
    ) -> None:
        missing = ClientError(
            {"Error": {"Code": "ChangeSetNotFound", "Message": "missing"}},
            "DescribeChangeSet",
        )
        stack_style_missing = ClientError(
            {"Error": {"Code": "ValidationError", "Message": "does not exist"}},
            "DescribeChangeSet",
        )
        assert manager._change_set_missing(missing) is True
        assert manager._change_set_missing(stack_style_missing) is False

        cfn = MagicMock()
        cfn.describe_change_set.side_effect = missing
        target = (
            self._REGION,
            cfn,
            {"StackId": self._STACK_ID, "StackStatus": "UPDATE_COMPLETE"},
        )
        authorize = MagicMock()
        prepared = MagicMock()
        with (
            patch.object(manager, "_get_deploy_region", return_value=self._REGION),
            patch("boto3.client", return_value=cfn),
            patch.object(manager, "_describe_stack_target", return_value=target),
        ):
            result = manager._execute_prepared_change_set(
                stack_name=self._STACK_NAME,
                change_set_name=self._CHANGE_SET_NAME,
                expected_stack_id=self._STACK_ID,
                expected_tags=self._RUN_TAGS,
                prepared_change_sets={},
                preparation_succeeded=True,
                authorize_stack=authorize,
                on_change_set_prepared=prepared,
                allow_noop=True,
                timeout=42,
            )

        assert result is True
        authorize.assert_called_once_with(self._STACK_NAME, self._REGION, self._STACK_ID)
        prepared.assert_not_called()
        cfn.execute_change_set.assert_not_called()

    def test_resumed_create_uses_persisted_type_and_exact_review_stack(
        self,
        manager: Any,
    ) -> None:
        cfn = MagicMock()
        cfn.describe_change_set.return_value = self._change_set()
        target = (
            self._REGION,
            cfn,
            {"StackId": self._STACK_ID, "StackStatus": "REVIEW_IN_PROGRESS"},
        )
        prepared = MagicMock()
        authorize = MagicMock()
        with (
            patch.object(manager, "_get_deploy_region", return_value=self._REGION),
            patch("boto3.client", return_value=cfn),
            patch.object(manager, "_describe_stack_target", return_value=target) as describe,
            patch.object(manager, "_wait_for_stack_settle", return_value="CREATE_COMPLETE"),
        ):
            result = manager._execute_prepared_change_set(
                stack_name=self._STACK_NAME,
                change_set_name=self._CHANGE_SET_NAME,
                expected_stack_id=self._STACK_ID,
                expected_tags=self._RUN_TAGS,
                prepared_change_sets=self._prepared_authority("CREATE"),
                preparation_succeeded=False,
                authorize_stack=authorize,
                on_change_set_prepared=prepared,
                allow_noop=False,
                timeout=42,
            )

        assert result is True
        describe.assert_called_once_with(
            self._STACK_NAME,
            expected_stack_id=self._STACK_ID,
            require_expected_identity=True,
        )
        prepared.assert_called_once_with(
            self._STACK_NAME,
            self._REGION,
            self._STACK_ID,
            self._CHANGE_SET_ID,
            "CREATE",
        )
        cfn.execute_change_set.assert_called_once_with(ChangeSetName=self._CHANGE_SET_ID)

    @pytest.mark.parametrize("expected_stack_id", (None, _STACK_ID))
    def test_executed_change_set_without_prior_checkpoint_is_rejected(
        self,
        manager: Any,
        expected_stack_id: str | None,
    ) -> None:
        cfn = MagicMock()
        cfn.describe_change_set.return_value = self._change_set(execution_status="EXECUTE_COMPLETE")
        prepared = MagicMock()
        authorize = MagicMock()
        with (
            patch.object(manager, "_get_deploy_region", return_value=self._REGION),
            patch("boto3.client", return_value=cfn),
            pytest.raises(RuntimeError, match="lacks prior checkpoint authority"),
        ):
            manager._execute_prepared_change_set(
                stack_name=self._STACK_NAME,
                change_set_name=self._CHANGE_SET_NAME,
                expected_stack_id=expected_stack_id,
                expected_tags=self._RUN_TAGS,
                prepared_change_sets={},
                preparation_succeeded=True,
                authorize_stack=authorize,
                on_change_set_prepared=prepared,
                allow_noop=False,
                timeout=42,
            )

        authorize.assert_not_called()
        prepared.assert_not_called()
        cfn.execute_change_set.assert_not_called()

    def test_executed_create_resume_revalidates_before_checkpoint(
        self,
        manager: Any,
    ) -> None:
        cfn = MagicMock()
        cfn.describe_change_set.return_value = self._change_set(execution_status="EXECUTE_COMPLETE")
        target = (
            self._REGION,
            cfn,
            {"StackId": self._STACK_ID, "StackStatus": "CREATE_COMPLETE"},
        )
        prepared = MagicMock()
        with (
            patch.object(manager, "_get_deploy_region", return_value=self._REGION),
            patch("boto3.client", return_value=cfn),
            patch.object(manager, "_describe_stack_target", return_value=target) as describe,
        ):
            result = manager._execute_prepared_change_set(
                stack_name=self._STACK_NAME,
                change_set_name=self._CHANGE_SET_NAME,
                expected_stack_id=self._STACK_ID,
                expected_tags=self._RUN_TAGS,
                prepared_change_sets=self._prepared_authority("CREATE"),
                preparation_succeeded=False,
                authorize_stack=MagicMock(),
                on_change_set_prepared=prepared,
                allow_noop=False,
                timeout=42,
            )

        assert result is True
        describe.assert_called_once_with(
            self._STACK_NAME,
            expected_stack_id=self._STACK_ID,
            require_expected_identity=True,
        )
        prepared.assert_called_once_with(
            self._STACK_NAME,
            self._REGION,
            self._STACK_ID,
            self._CHANGE_SET_ID,
            "CREATE",
        )
        cfn.execute_change_set.assert_not_called()


class TestStrictOrchestrationPreflight:
    _GLOBAL_ID = "arn:aws:cloudformation:us-east-2:123456789012:stack/gco-global/global-uuid"
    _REGIONAL_ID = "arn:aws:cloudformation:us-east-2:123456789012:stack/gco-us-east-2/regional-uuid"
    _BOOTSTRAP = {
        "us-east-2": {
            "stack_id": (
                "arn:aws:cloudformation:us-east-2:123456789012:stack/CDKToolkit/toolkit-uuid"
            ),
            "status": "CREATE_COMPLETE",
        }
    }

    def test_every_target_is_validated_before_first_deploy(self, manager: Any) -> None:
        stacks = ["gco-global", "gco-us-east-2"]
        expected = {
            "gco-global": self._GLOBAL_ID,
            "gco-us-east-2": self._REGIONAL_ID,
        }
        events: list[str] = []

        def describe(name: str, **_kwargs: Any) -> tuple[str, Any, dict[str, str]]:
            events.append(f"target:{name}")
            if name == "gco-us-east-2":
                raise RuntimeError("replacement")
            return (
                "us-east-2",
                MagicMock(),
                {"StackId": expected[name], "StackStatus": "CREATE_COMPLETE"},
            )

        with (
            patch.object(manager, "list_stacks", return_value=stacks),
            patch.object(manager, "_get_deploy_region", return_value="us-east-2"),
            patch.object(
                manager,
                "_validate_bootstrap_stack",
                side_effect=lambda *_args: events.append("bootstrap"),
            ),
            patch.object(manager, "_describe_stack_target", side_effect=describe),
            patch.object(manager, "deploy") as deploy,
            pytest.raises(RuntimeError, match="replacement"),
        ):
            manager.deploy_orchestrated(
                require_approval=False,
                allow_bootstrap=False,
                bootstrap_stacks=self._BOOTSTRAP,
                expected_stack_ids=expected,
                prepared_change_sets={name: {} for name in stacks},
                authorize_stack=MagicMock(),
                strict_deployment_token="run-123",
                on_change_set_prepared=MagicMock(),
            )

        assert events == ["bootstrap", "target:gco-global", "target:gco-us-east-2"]
        deploy.assert_not_called()

    def test_strict_deploy_builds_deterministic_prepare_command(self, manager: Any) -> None:
        prepared = MagicMock()
        authorize = MagicMock()
        expected = {"gco-global": None}
        with (
            patch.object(manager, "_sync_lambda_sources"),
            patch.object(manager, "_ensure_lambda_build"),
            patch("cli.stacks._detect_container_runtime", return_value="docker"),
            patch.object(manager, "_get_deploy_region", return_value="us-east-2"),
            patch.object(manager, "_validate_bootstrap_stack"),
            patch.object(manager, "_describe_stack_target", return_value=None),
            patch.object(manager, "_preflight_strict_change_set") as preflight,
            patch.object(manager, "_mirror_images_if_enabled"),
            patch.object(manager, "_run_cdk", return_value=MagicMock(returncode=0)) as run,
            patch.object(
                manager,
                "_execute_prepared_change_set",
                return_value=True,
            ) as execute,
        ):
            assert (
                manager.deploy(
                    stack_name="gco-global",
                    require_approval=False,
                    allow_bootstrap=False,
                    bootstrap_stacks=self._BOOTSTRAP,
                    expected_stack_ids=expected,
                    prepared_change_sets={"gco-global": {}},
                    authorize_stack=authorize,
                    strict_deployment_token="run-123",
                    on_change_set_prepared=prepared,
                )
                is True
            )

        command = run.call_args.args[0]
        change_set_name = manager._strict_change_set_name("gco-global", "run-123")
        preflight.assert_called_once_with(
            stack_name="gco-global",
            change_set_name=change_set_name,
            expected_stack_id=None,
            prepared_change_sets={},
        )
        assert execute.call_args.kwargs["preparation_succeeded"] is True
        assert command[:2] == ["deploy", "gco-global"]
        assert command[command.index("--method") : command.index("--method") + 4] == [
            "--method",
            "prepare-change-set",
            "--change-set-name",
            change_set_name,
        ]

    @pytest.mark.parametrize("expected_stack_id", (None, _GLOBAL_ID))
    def test_preexisting_uncheckpointed_change_set_blocks_before_local_mutation(
        self,
        manager: Any,
        expected_stack_id: str | None,
    ) -> None:
        stack_name = "gco-global"
        token = "run-123"
        change_set_name = manager._strict_change_set_name(stack_name, token)
        change_set_id = (
            "arn:aws:cloudformation:us-east-2:123456789012:"
            f"changeSet/{change_set_name}/change-set-uuid"
        )
        cfn = MagicMock()
        cfn.describe_change_set.return_value = {
            "ChangeSetName": change_set_name,
            "ChangeSetId": change_set_id,
            "StackId": self._GLOBAL_ID,
            "Status": "CREATE_COMPLETE",
            "ExecutionStatus": "AVAILABLE",
            "Tags": [{"Key": "GcoLiveValidationRun", "Value": token}],
        }
        target = (
            None
            if expected_stack_id is None
            else (
                "us-east-2",
                cfn,
                {"StackId": self._GLOBAL_ID, "StackStatus": "UPDATE_COMPLETE"},
            )
        )
        with (
            patch.object(manager, "_sync_lambda_sources"),
            patch.object(manager, "_ensure_lambda_build"),
            patch.object(manager, "_get_deploy_region", return_value="us-east-2"),
            patch.object(manager, "_validate_bootstrap_stack"),
            patch.object(manager, "_describe_stack_target", return_value=target),
            patch("boto3.client", return_value=cfn),
            patch.object(manager, "_mirror_images_if_enabled") as mirror,
            patch.object(manager, "_run_cdk") as run,
            pytest.raises(RuntimeError, match="lacks checkpoint authority"),
        ):
            manager.deploy(
                stack_name=stack_name,
                require_approval=False,
                tags={"GcoLiveValidationRun": token},
                allow_bootstrap=False,
                bootstrap_stacks=self._BOOTSTRAP,
                expected_stack_ids={stack_name: expected_stack_id},
                prepared_change_sets={stack_name: {}},
                authorize_stack=MagicMock(),
                strict_deployment_token=token,
                on_change_set_prepared=MagicMock(),
            )

        mirror.assert_not_called()
        run.assert_not_called()

    @pytest.mark.parametrize("expected_stack_id", (None, _GLOBAL_ID))
    @pytest.mark.parametrize("outcome", ("nonzero", "timeout"))
    def test_failed_preparation_cannot_mint_available_authority(
        self,
        manager: Any,
        expected_stack_id: str | None,
        outcome: str,
    ) -> None:
        stack_name = "gco-global"
        token = "run-123"
        change_set_name = manager._strict_change_set_name(stack_name, token)
        change_set_id = (
            "arn:aws:cloudformation:us-east-2:123456789012:"
            f"changeSet/{change_set_name}/change-set-uuid"
        )
        cfn = MagicMock()
        cfn.describe_change_set.return_value = {
            "ChangeSetName": change_set_name,
            "ChangeSetId": change_set_id,
            "StackId": self._GLOBAL_ID,
            "Status": "CREATE_COMPLETE",
            "ExecutionStatus": "AVAILABLE",
            "Tags": [{"Key": "GcoLiveValidationRun", "Value": token}],
        }
        target = (
            None
            if expected_stack_id is None
            else (
                "us-east-2",
                cfn,
                {"StackId": self._GLOBAL_ID, "StackStatus": "UPDATE_COMPLETE"},
            )
        )
        run_result = MagicMock(returncode=1)
        run_error = (
            subprocess.TimeoutExpired(cmd="cdk deploy", timeout=3600)
            if outcome == "timeout"
            else None
        )
        prepared = MagicMock()
        authorize = MagicMock()
        with (
            patch.object(manager, "_sync_lambda_sources"),
            patch.object(manager, "_ensure_lambda_build"),
            patch("cli.stacks._detect_container_runtime", return_value="docker"),
            patch.object(manager, "_get_deploy_region", return_value="us-east-2"),
            patch.object(manager, "_validate_bootstrap_stack"),
            patch.object(manager, "_describe_stack_target", return_value=target),
            patch.object(manager, "_preflight_strict_change_set"),
            patch.object(manager, "_mirror_images_if_enabled"),
            patch.object(
                manager,
                "_run_cdk",
                return_value=run_result,
                side_effect=run_error,
            ),
            patch("boto3.client", return_value=cfn),
            patch.object(manager, "_diagnose_deploy_failure"),
            pytest.raises(RuntimeError, match="not produced by this preparation"),
        ):
            manager.deploy(
                stack_name=stack_name,
                require_approval=False,
                tags={"GcoLiveValidationRun": token},
                allow_bootstrap=False,
                bootstrap_stacks=self._BOOTSTRAP,
                expected_stack_ids={stack_name: expected_stack_id},
                prepared_change_sets={stack_name: {}},
                authorize_stack=authorize,
                strict_deployment_token=token,
                on_change_set_prepared=prepared,
            )

        authorize.assert_not_called()
        prepared.assert_not_called()
        cfn.execute_change_set.assert_not_called()

    def test_strict_change_set_names_are_cloudformation_safe_and_distinct(
        self,
        manager: Any,
    ) -> None:
        tokens = (
            "20260718T183518Z-deadbeef",
            "123-leading-digit",
            "é/unicode token",
            "run-123",
            "run-123-analytics-routes",
            "run-123-teardown-drop-analytics-routes",
        )
        names = [manager._strict_change_set_name("gco-global", token) for token in tokens]

        assert len(names) == len(set(names))
        for name in names:
            assert re.fullmatch(r"[A-Za-z][A-Za-z0-9-]{0,127}", name)
            assert name.startswith("gco-")
            assert len(name) <= 128

    def test_incomplete_teardown_map_fails_before_any_helper(self, manager: Any) -> None:
        stacks = ["gco-global", "gco-us-east-2"]
        with (
            patch.object(manager, "list_stacks", return_value=stacks),
            patch.object(manager, "_resolve_strict_teardown_resources") as resolve,
            patch.object(manager, "_image_registry_destroy_preflight") as image_preflight,
            patch.object(manager, "cleanup_orphaned_bastions") as bastions,
            pytest.raises(RuntimeError, match="target map is incomplete"),
        ):
            manager.destroy_orchestrated(
                force=True,
                expected_stack_ids={"gco-global": self._GLOBAL_ID},
                authorize_stack=MagicMock(),
                allow_bootstrap=False,
                bootstrap_stacks=self._BOOTSTRAP,
            )

        resolve.assert_not_called()
        image_preflight.assert_not_called()
        bastions.assert_not_called()

    def test_authoritative_absence_rejects_same_name_replacement(self, manager: Any) -> None:
        replacement_id = (
            "arn:aws:cloudformation:us-east-2:123456789012:stack/gco-global/replacement"
        )
        cfn = MagicMock()
        cfn.describe_stacks.return_value = {
            "Stacks": [
                {
                    "StackName": "gco-global",
                    "StackId": replacement_id,
                    "StackStatus": "CREATE_COMPLETE",
                }
            ]
        }
        with (
            patch.object(manager, "_get_destroy_region", return_value="us-east-2"),
            patch("boto3.client", return_value=cfn),
            pytest.raises(RuntimeError, match="uncheckpointed stack"),
        ):
            manager._describe_stack_target(
                "gco-global",
                expected_stack_id=None,
                require_expected_identity=True,
            )


class TestStrictTeardownHelpers:
    _REGION = "us-east-2"
    _STACK_NAME = "gco-us-east-2"
    _STACK_ID = "arn:aws:cloudformation:us-east-2:123456789012:stack/gco-us-east-2/regional-uuid"

    def test_resolves_vpc_cluster_and_security_group_from_exact_stack(
        self,
        manager: Any,
    ) -> None:
        cfn = MagicMock()
        paginator = MagicMock()
        paginator.paginate.return_value = [
            {
                "StackResourceSummaries": [
                    {
                        "ResourceType": "AWS::EC2::VPC",
                        "PhysicalResourceId": "vpc-exact",
                    },
                    {
                        "ResourceType": "AWS::EKS::Cluster",
                        "PhysicalResourceId": "gco-us-east-2",
                    },
                ]
            }
        ]
        cfn.get_paginator.return_value = paginator
        stack = {
            "StackName": self._STACK_NAME,
            "StackId": self._STACK_ID,
            "StackStatus": "CREATE_COMPLETE",
        }
        eks = MagicMock()
        eks.describe_cluster.return_value = {
            "cluster": {
                "name": self._STACK_NAME,
                "resourcesVpcConfig": {
                    "vpcId": "vpc-exact",
                    "clusterSecurityGroupId": "sg-exact",
                },
            }
        }
        authorize = MagicMock()
        with (
            patch.object(
                manager,
                "_describe_stack_target",
                return_value=(self._REGION, cfn, stack),
            ),
            patch.object(manager, "_get_deploy_region", return_value=self._REGION),
            patch("boto3.client", return_value=eks),
        ):
            result = manager._resolve_strict_teardown_resources(
                stacks=[self._STACK_NAME],
                regional_stacks=[self._STACK_NAME],
                expected_stack_ids={self._STACK_NAME: self._STACK_ID},
                authorize_stack=authorize,
            )

        assert result[self._STACK_NAME] == {
            "stack_name": self._STACK_NAME,
            "stack_id": self._STACK_ID,
            "region": self._REGION,
            "vpc_id": "vpc-exact",
            "cluster_name": self._STACK_NAME,
            "cluster_security_group_id": "sg-exact",
        }
        authorize.assert_called_once_with(self._STACK_NAME, self._REGION, self._STACK_ID)
        paginator.paginate.assert_called_once_with(StackName=self._STACK_ID)
        eks.describe_cluster.assert_called_once_with(name=self._STACK_NAME)

    def test_exact_helper_targets_are_used_and_sg_errors_block_progress(
        self,
        manager: Any,
    ) -> None:
        details = {
            "stack_name": self._STACK_NAME,
            "stack_id": self._STACK_ID,
            "region": self._REGION,
            "vpc_id": "vpc-exact",
            "cluster_name": self._STACK_NAME,
            "cluster_security_group_id": "sg-exact",
        }
        thread = MagicMock()
        thread.is_alive.return_value = False
        authorize = MagicMock()
        with (
            patch.object(manager, "list_stacks", return_value=[self._STACK_NAME]),
            patch.object(
                manager,
                "_resolve_strict_teardown_resources",
                return_value={self._STACK_NAME: details},
            ),
            patch.object(manager, "_image_registry_destroy_preflight", return_value=True),
            patch.object(manager, "cleanup_orphaned_bastions", return_value=1) as bastions,
            patch.object(manager, "_cleanup_backup_vault", return_value={"errors": []}),
            patch.object(manager, "destroy", return_value=True),
            patch.object(manager, "_destroy_phase_remaining_stacks", return_value=[]),
            patch.object(manager, "_start_eks_sg_watchdog", return_value=thread) as watchdog,
            patch.object(
                manager,
                "_cleanup_eks_security_groups",
                return_value={"errors": ["denied"], "blocked_by_enis": []},
            ) as cleanup_sg,
        ):
            overall, successful, failed = manager.destroy_orchestrated(
                force=True,
                parallel=False,
                expected_stack_ids={self._STACK_NAME: self._STACK_ID},
                prepared_change_sets={self._STACK_NAME: {}},
                authorize_stack=authorize,
                allow_bootstrap=False,
                bootstrap_stacks={},
                strict_deployment_token="run-123-teardown",
                on_change_set_prepared=MagicMock(),
            )

        assert overall is False
        assert successful == [self._STACK_NAME]
        assert failed == [self._STACK_NAME]
        bastions.assert_called_once_with(
            [self._STACK_NAME],
            parallel=False,
            resource_targets={self._STACK_NAME: details},
        )
        watchdog.assert_called_once_with(
            self._STACK_NAME,
            watchdog.call_args.args[1],
            region=self._REGION,
            security_group_id="sg-exact",
            vpc_id="vpc-exact",
        )
        cleanup_sg.assert_called_once_with(
            self._STACK_NAME,
            region=self._REGION,
            security_group_id="sg-exact",
            vpc_id="vpc-exact",
        )

    def test_strict_bastion_failure_prevents_stack_deletion(self, manager: Any) -> None:
        details = {
            "stack_name": self._STACK_NAME,
            "stack_id": self._STACK_ID,
            "region": self._REGION,
            "vpc_id": "vpc-exact",
        }
        with (
            patch.object(manager, "list_stacks", return_value=[self._STACK_NAME]),
            patch.object(
                manager,
                "_resolve_strict_teardown_resources",
                return_value={self._STACK_NAME: details},
            ),
            patch.object(manager, "_image_registry_destroy_preflight", return_value=True),
            patch.object(
                manager,
                "cleanup_orphaned_bastions",
                side_effect=RuntimeError("bastion inspection denied"),
            ),
            patch.object(manager, "destroy") as destroy,
            pytest.raises(RuntimeError, match="bastion inspection denied"),
        ):
            manager.destroy_orchestrated(
                force=True,
                expected_stack_ids={self._STACK_NAME: self._STACK_ID},
                authorize_stack=MagicMock(),
            )

        destroy.assert_not_called()


class TestStrictDestroyIdentityFence:
    _STACK_NAME = "gco-monitoring"
    _STACK_ID = "arn:aws:cloudformation:us-east-2:123456789012:stack/gco-monitoring/monitoring-uuid"
    _ANALYTICS_ID = (
        "arn:aws:cloudformation:us-east-2:123456789012:stack/gco-analytics/analytics-uuid"
    )

    def test_single_expected_identity_deletes_only_the_exact_arn(self, manager: Any) -> None:
        authorize = MagicMock()
        with (
            patch.object(manager, "_cloudformation_delete_stack", return_value=True) as delete,
            patch.object(manager, "_run_cdk") as run_cdk,
        ):
            result = manager._destroy(
                stack_name=self._STACK_NAME,
                force=True,
                expected_stack_id=self._STACK_ID,
                authorize_stack=authorize,
            )

        assert result is True
        delete.assert_called_once_with(
            self._STACK_NAME,
            expected_stack_id=self._STACK_ID,
            authorize_stack=authorize,
            require_expected_identity=True,
        )
        run_cdk.assert_not_called()

    def test_all_stacks_rejects_identity_authority(self, manager: Any) -> None:
        with (
            patch.object(manager, "_run_cdk") as run_cdk,
            pytest.raises(RuntimeError, match="cannot use all_stacks=True"),
        ):
            manager._destroy(
                all_stacks=True,
                expected_stack_ids={self._STACK_NAME: self._STACK_ID},
                authorize_stack=MagicMock(),
            )

        run_cdk.assert_not_called()

    def test_single_identity_cannot_teardown_analytics_dependencies(
        self,
        manager: Any,
    ) -> None:
        with (
            patch.object(manager, "_remove_api_gateway_analytics_dependency") as remove,
            patch.object(manager, "_cloudformation_delete_stack") as delete,
            pytest.raises(RuntimeError, match="complete expected stack identity map"),
        ):
            manager._destroy(
                stack_name="gco-analytics",
                force=True,
                expected_stack_id=self._ANALYTICS_ID,
                authorize_stack=MagicMock(),
            )

        remove.assert_not_called()
        delete.assert_not_called()


class TestStrictAnalyticsTeardown:
    _ANALYTICS_ID = (
        "arn:aws:cloudformation:us-east-2:123456789012:stack/gco-analytics/analytics-uuid"
    )
    _API_ID = "arn:aws:cloudformation:us-east-2:123456789012:stack/gco-api-gateway/api-uuid"

    def test_analytics_destroy_uses_distinct_prepared_dependency_token(
        self,
        manager: Any,
    ) -> None:
        expected = {
            "gco-analytics": self._ANALYTICS_ID,
            "gco-api-gateway": self._API_ID,
        }
        prepared_change_sets = {name: {} for name in expected}
        authorize = MagicMock()
        prepared = MagicMock()
        repository_created = MagicMock()
        with (
            patch.object(
                manager,
                "_remove_api_gateway_analytics_dependency",
                return_value=True,
            ) as remove_dependency,
            patch.object(manager, "_cloudformation_delete_stack", return_value=True) as delete,
        ):
            result = manager._destroy(
                stack_name="gco-analytics",
                force=True,
                expected_stack_id=self._ANALYTICS_ID,
                expected_stack_ids=expected,
                prepared_change_sets=prepared_change_sets,
                authorize_stack=authorize,
                allow_bootstrap=False,
                bootstrap_stacks={},
                strict_deployment_token="run-123-teardown",
                on_change_set_prepared=prepared,
                on_ecr_repository_created=repository_created,
            )

        assert result is True
        kwargs = remove_dependency.call_args.kwargs
        assert kwargs["strict_deployment_token"] == ("run-123-teardown-drop-analytics-routes")
        assert kwargs["expected_stack_ids"] is expected
        assert kwargs["prepared_change_sets"] is prepared_change_sets
        assert kwargs["authorize_stack"] is authorize
        assert kwargs["on_change_set_prepared"] is prepared
        assert kwargs["on_ecr_repository_created"] is repository_created
        delete.assert_called_once_with(
            "gco-analytics",
            expected_stack_id=self._ANALYTICS_ID,
            authorize_stack=authorize,
            require_expected_identity=True,
        )

    def test_dependency_update_forwards_strict_change_set_authority(
        self,
        manager: Any,
    ) -> None:
        expected = {
            "gco-analytics": self._ANALYTICS_ID,
            "gco-api-gateway": self._API_ID,
        }
        prepared_change_sets = {name: {} for name in expected}
        authorize = MagicMock()
        prepared = MagicMock()
        repository_created = MagicMock()
        with (
            patch.object(manager, "_stack_exists_in_cloudformation", return_value=True),
            patch.object(manager, "_api_gateway_imports_from_analytics", return_value=True),
            patch("cli.stacks.get_analytics_config", return_value={"enabled": False}),
            patch.object(manager, "deploy", return_value=True) as deploy,
        ):
            assert (
                manager._remove_api_gateway_analytics_dependency(
                    allow_bootstrap=False,
                    bootstrap_stacks={},
                    expected_stack_ids=expected,
                    prepared_change_sets=prepared_change_sets,
                    authorize_stack=authorize,
                    strict_deployment_token="run-123-teardown-drop-analytics-routes",
                    on_change_set_prepared=prepared,
                    on_ecr_repository_created=repository_created,
                )
                is True
            )

        kwargs = deploy.call_args.kwargs
        assert kwargs["stack_name"] == "gco-api-gateway"
        assert kwargs["expected_stack_ids"] is expected
        assert kwargs["prepared_change_sets"] is prepared_change_sets
        assert kwargs["strict_deployment_token"] == ("run-123-teardown-drop-analytics-routes")
        assert kwargs["authorize_stack"] is authorize
        assert kwargs["on_change_set_prepared"] is prepared
        assert kwargs["on_ecr_repository_created"] is repository_created


class TestCdkWorkerCancellation:
    def test_cancel_active_cdk_processes_terminates_every_registered_process(
        self,
        manager: Any,
    ) -> None:
        first = MagicMock()
        second = MagicMock()
        manager._active_cdk_processes = {101: first, 202: second}
        with patch.object(manager, "_terminate_cdk_process") as terminate:
            manager.cancel_active_cdk_processes()

        assert manager._cdk_cancel_event.is_set()
        assert terminate.call_args_list == [
            ((first,), {}),
            ((second,), {}),
        ]

    def test_windows_termination_targets_the_complete_process_tree(self, manager: Any) -> None:
        process = MagicMock(pid=4321)
        process.poll.return_value = None
        process.wait.return_value = 0
        taskkill_result = MagicMock(returncode=0)

        with (
            patch("cli.stacks.os.name", "nt"),
            patch("cli.stacks.shutil.which", return_value="C:/Windows/System32/taskkill.exe"),
            patch("cli.stacks.subprocess.run", return_value=taskkill_result) as run,
        ):
            manager._terminate_cdk_process(process)

        command = run.call_args.args[0]
        assert command == [
            "C:/Windows/System32/taskkill.exe",
            "/PID",
            "4321",
            "/T",
        ]
        process.terminate.assert_not_called()
        process.kill.assert_not_called()

    def test_windows_termination_force_kills_the_tree_after_grace_timeout(
        self,
        manager: Any,
    ) -> None:
        import subprocess

        process = MagicMock(pid=4321)
        process.poll.side_effect = [None, 0]
        process.wait.side_effect = [
            subprocess.TimeoutExpired(cmd="cdk", timeout=30),
            0,
        ]
        with (
            patch("cli.stacks.os.name", "nt"),
            patch("cli.stacks.shutil.which", return_value="taskkill.exe"),
            patch(
                "cli.stacks.subprocess.run",
                return_value=MagicMock(returncode=0),
            ) as run,
        ):
            manager._terminate_cdk_process(process)

        assert run.call_count == 2
        assert run.call_args_list[0].args[0] == [
            "taskkill.exe",
            "/PID",
            "4321",
            "/T",
        ]
        assert run.call_args_list[1].args[0] == [
            "taskkill.exe",
            "/PID",
            "4321",
            "/T",
            "/F",
        ]
        process.kill.assert_not_called()

    def test_windows_cdk_process_uses_a_dedicated_process_group(self, manager: Any) -> None:
        manager._cdk_path = "cdk"
        process = MagicMock(pid=4321, returncode=0)
        process.communicate.return_value = ("", "")
        with (
            patch.object(manager, "_ensure_cdk_toolchain"),
            patch.object(manager, "_ensure_lambda_build"),
            patch("cli.stacks.os.name", "nt"),
            patch(
                "cli.stacks.subprocess.CREATE_NEW_PROCESS_GROUP",
                512,
                create=True,
            ),
            patch("cli.stacks.subprocess.Popen", return_value=process) as popen,
        ):
            manager._run_cdk(["list"])

        assert popen.call_args.kwargs["creationflags"] == 512
        assert popen.call_args.kwargs["start_new_session"] is False

    def test_parallel_interrupt_cancels_before_executor_wait(self, manager: Any) -> None:
        events: list[str] = []

        class Future:
            def result(self) -> tuple[str, bool]:
                events.append("result")
                raise KeyboardInterrupt

            def cancel(self) -> bool:
                events.append("future-cancel")
                return True

        future = Future()

        class Executor:
            def submit(self, _fn: Any, _stack: str) -> Future:
                return future

            def shutdown(self, *, wait: bool, cancel_futures: bool = False) -> None:
                events.append(f"shutdown:{wait}:{cancel_futures}")

        with (
            patch("cli.stacks.ThreadPoolExecutor", return_value=Executor()),
            patch("cli.stacks.as_completed", return_value=[future]),
            patch.object(
                manager,
                "cancel_active_cdk_processes",
                side_effect=lambda: events.append("cancel-processes"),
            ),
            pytest.raises(KeyboardInterrupt),
        ):
            manager._deploy_stacks_parallel(
                stacks=["gco-us-east-2"],
                require_approval=False,
                outputs_file=None,
                parameters=None,
                tags=None,
                progress="events",
                on_stack_start=None,
                on_stack_complete=None,
                max_workers=1,
                allow_bootstrap=False,
                bootstrap_stacks={},
                expected_stack_ids={"gco-us-east-2": None},
                prepared_change_sets={"gco-us-east-2": {}},
                authorize_stack=MagicMock(),
            )

        assert events == [
            "result",
            "cancel-processes",
            "future-cancel",
            "shutdown:True:True",
        ]
        assert not manager._cdk_cancel_event.is_set()
