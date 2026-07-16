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
    def test_stack_exists_in_cloudformation_true(self, manager: Any) -> None:
        cfn = MagicMock()
        cfn.describe_stacks.return_value = {"Stacks": [{"StackStatus": "CREATE_COMPLETE"}]}
        with patch("boto3.client", return_value=cfn):
            assert manager._stack_exists_in_cloudformation("gco-global") is True

    def test_stack_exists_returns_false_on_delete_status(self, manager: Any) -> None:
        cfn = MagicMock()
        cfn.describe_stacks.return_value = {"Stacks": [{"StackStatus": "DELETE_COMPLETE"}]}
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
        with patch("boto3.client", return_value=cfn):
            assert manager._cloudformation_delete_stack("gco-global") is True
        cfn.delete_stack.assert_called_once()
        cfn.get_waiter.assert_called_once_with("stack_delete_complete")

    def test_cloudformation_delete_stack_failure(self, manager: Any) -> None:
        cfn = MagicMock()
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
    def test_finds_vault_and_deletes_recovery_points(self, manager: Any, capsys: Any) -> None:
        backup = MagicMock()

        def get_paginator(op: str) -> Any:
            if op == "list_backup_vaults":
                return _make_paginator(
                    [
                        {
                            "BackupVaultList": [
                                {"BackupVaultName": "GcoBackupVault"},
                                {"BackupVaultName": "OtherVault"},
                            ]
                        }
                    ]
                )
            return _make_paginator(
                [
                    {
                        "RecoveryPoints": [
                            {"RecoveryPointArn": "arn:aws:backup:rp1"},
                            {"RecoveryPointArn": "arn:aws:backup:rp2"},
                        ]
                    }
                ]
            )

        backup.get_paginator.side_effect = get_paginator
        with patch("boto3.client", return_value=backup):
            manager._cleanup_backup_vault()
        # Two delete_recovery_point calls.
        assert backup.delete_recovery_point.call_count == 2
        captured = capsys.readouterr().out
        assert "Cleaned up 2 backup recovery points" in captured

    def test_no_vault_short_circuits(self, manager: Any) -> None:
        backup = MagicMock()
        backup.get_paginator.return_value = _make_paginator([{"BackupVaultList": []}])
        with patch("boto3.client", return_value=backup):
            manager._cleanup_backup_vault()
        backup.delete_recovery_point.assert_not_called()

    def test_swallows_top_level_exceptions(self, manager: Any, capsys: Any) -> None:
        backup = MagicMock()
        backup.get_paginator.side_effect = RuntimeError("denied")
        with patch("boto3.client", return_value=backup):
            manager._cleanup_backup_vault()
        out = capsys.readouterr().out
        assert "Backup vault cleanup failed" in out

    def test_recovery_point_delete_failure_logged(self, manager: Any) -> None:
        backup = MagicMock()

        def get_paginator(op: str) -> Any:
            if op == "list_backup_vaults":
                return _make_paginator([{"BackupVaultList": [{"BackupVaultName": "GcoVault"}]}])
            return _make_paginator(
                [
                    {
                        "RecoveryPoints": [
                            {"RecoveryPointArn": "arn:aws:backup:rp1"},
                        ]
                    }
                ]
            )

        backup.get_paginator.side_effect = get_paginator
        backup.delete_recovery_point.side_effect = RuntimeError("denied")
        with patch("boto3.client", return_value=backup):
            # Must not raise.
            manager._cleanup_backup_vault()


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

    def test_deletes_orphaned_eni_then_sg(self, manager: Any, capsys: Any) -> None:
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
                    "Attachment": {"AttachmentId": "eni-attach-1"},
                },
                {"NetworkInterfaceId": "eni-2"},
            ]
        }
        with (
            patch("boto3.client", return_value=ec2),
            patch("time.sleep"),
        ):
            manager._cleanup_eks_security_groups("gco-us-east-1")
        ec2.detach_network_interface.assert_called_once_with(
            AttachmentId="eni-attach-1", Force=True
        )
        # delete_network_interface for each ENI
        assert ec2.delete_network_interface.call_count == 2
        ec2.delete_security_group.assert_called_once_with(GroupId="sg-123")
        out = capsys.readouterr().out
        assert "Cleaned up EKS security group" in out

    def test_eni_delete_failure_does_not_block_sg_delete(self, manager: Any) -> None:
        ec2 = MagicMock()
        ec2.describe_security_groups.return_value = {
            "SecurityGroups": [{"GroupId": "sg-x", "GroupName": "eks-cluster-sg-x"}]
        }
        ec2.describe_network_interfaces.return_value = {
            "NetworkInterfaces": [{"NetworkInterfaceId": "eni-1"}]
        }
        ec2.delete_network_interface.side_effect = RuntimeError("denied")
        with patch("boto3.client", return_value=ec2):
            manager._cleanup_eks_security_groups("gco-us-east-1")
        ec2.delete_security_group.assert_called_once()

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
