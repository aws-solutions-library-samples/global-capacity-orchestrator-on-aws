"""Tests for the destroy/deploy hardening in cli/stacks.py.

Two behaviours land here:

1. State-aware deploy verification — ``deploy()`` no longer trusts cdk's exit
   code alone on success; it confirms CloudFormation actually shows a terminal
   ``CREATE_COMPLETE`` / ``UPDATE_COMPLETE`` so a silent rollback can't read as a
   successful deploy. An unknown status (``None``) leaves cdk's verdict intact.

2. Orphaned-ENI sweep — the between-retry cleanup is generalized from "EKS
   cluster SG + its ENIs" to a report-and-clear pass over every network
   interface still in a regional stack's VPC, categorized (Global Accelerator /
   ELB / EKS / other). Detached, non-service-managed interfaces are deleted;
   service-managed ones (released asynchronously by AWS) are reported, not
   fought.
"""

from pathlib import Path
from unittest.mock import MagicMock, patch


class TestDeploySuccessStateVerification:
    """deploy() reconciles a cdk 'success' against the real CloudFormation status."""

    def _deploy(self, cfn_status, *, returncode=0, all_stacks=False, stack="gco-global"):
        from cli.stacks import StackManager

        config = MagicMock()
        config.global_region = "us-east-2"
        get_status = MagicMock(return_value=cfn_status)
        with (
            patch("cli.stacks._detect_container_runtime", return_value="docker"),
            patch.object(StackManager, "_check_and_fix_stuck_stack"),
            patch.object(StackManager, "ensure_bootstrapped", return_value=True),
            patch.object(StackManager, "_run_cdk") as mock_run,
            patch.object(StackManager, "_get_stack_status", get_status),
            patch.object(StackManager, "_diagnose_deploy_failure"),
        ):
            mock_run.return_value = MagicMock(returncode=returncode)
            manager = StackManager(config)
            if all_stacks:
                result = manager.deploy(all_stacks=True, require_approval=False)
            else:
                result = manager.deploy(stack_name=stack, require_approval=False)
        return result, get_status

    def test_cdk_success_but_rollback_status_is_failure(self):
        """cdk exits 0 but CFN shows UPDATE_ROLLBACK_COMPLETE → failed deploy."""
        result, get_status = self._deploy("UPDATE_ROLLBACK_COMPLETE")
        assert result is False
        get_status.assert_called_once()

    def test_cdk_success_and_create_complete_is_success(self):
        result, _ = self._deploy("CREATE_COMPLETE")
        assert result is True

    def test_cdk_success_and_update_complete_is_success(self):
        result, _ = self._deploy("UPDATE_COMPLETE")
        assert result is True

    def test_cdk_success_and_unknown_status_trusts_cdk(self):
        """A None status (lookup failed / transient) must not override cdk's
        success — we only flip on a *known* non-terminal-success state."""
        result, _ = self._deploy(None)
        assert result is True

    def test_all_stacks_deploy_skips_status_verification(self):
        """--all has no single stack to reconcile, so _get_stack_status is not
        consulted even though the (mocked) status would be a failure state."""
        result, get_status = self._deploy("ROLLBACK_COMPLETE", all_stacks=True)
        assert result is True
        get_status.assert_not_called()


class TestClassifyOrphanedEni:
    """StackManager._classify_orphaned_eni buckets ENIs by owning service."""

    def _classify(self, **eni):
        from cli.stacks import StackManager

        return StackManager._classify_orphaned_eni(eni)

    def test_global_accelerator_by_interface_type(self):
        assert self._classify(InterfaceType="global_accelerator_managed") == "global_accelerator"

    def test_global_accelerator_by_description(self):
        assert (
            self._classify(
                InterfaceType="interface",
                Description="Network interface for Global Accelerator endpoint",
            )
            == "global_accelerator"
        )

    def test_elb_by_interface_type_alb(self):
        assert self._classify(InterfaceType="load_balancer") == "elb"

    def test_elb_by_interface_type_nlb(self):
        assert self._classify(InterfaceType="network_load_balancer") == "elb"

    def test_elb_by_description_prefix(self):
        assert (
            self._classify(InterfaceType="interface", Description="ELB app/k8s-gco-ingr/abc123")
            == "elb"
        )

    def test_eks_by_description(self):
        assert self._classify(InterfaceType="interface", Description="aws-K8S-i-0abc123") == "eks"

    def test_other_when_unrecognized(self):
        assert self._classify(InterfaceType="interface", Description="some random eni") == "other"

    def test_missing_fields_default_to_other(self):
        assert self._classify() == "other"


class TestSummarizeOrphanedEnis:
    """StackManager._summarize_orphaned_enis counts + safely clears VPC ENIs."""

    def _manager(self):
        from cli.stacks import StackManager

        config = MagicMock()
        config.project_name = "gco"
        manager = StackManager.__new__(StackManager)
        manager.config = config
        manager.project_root = Path(".")
        return manager

    def test_counts_categories_and_deletes_only_safe_enis(self):
        manager = self._manager()
        mock_ec2 = MagicMock()
        mock_ec2.describe_vpcs.return_value = {"Vpcs": [{"VpcId": "vpc-1"}]}
        mock_ec2.describe_network_interfaces.return_value = {
            "NetworkInterfaces": [
                # GA-managed, attached, service-owned → reported, not deleted.
                {
                    "NetworkInterfaceId": "eni-ga",
                    "InterfaceType": "global_accelerator_managed",
                    "Status": "in-use",
                    "RequesterManaged": True,
                },
                # ELB-managed, detached but service-owned → reported, not deleted.
                {
                    "NetworkInterfaceId": "eni-elb",
                    "Description": "ELB app/k8s-gco-ingr/abc",
                    "Status": "available",
                    "RequesterManaged": True,
                },
                # EKS, detached, user-managed → safe to delete.
                {
                    "NetworkInterfaceId": "eni-eks",
                    "Description": "aws-K8S-i-0abc",
                    "Status": "available",
                    "RequesterManaged": False,
                },
                # Other, attached → reported, not deleted.
                {
                    "NetworkInterfaceId": "eni-other",
                    "InterfaceType": "interface",
                    "Description": "random",
                    "Status": "in-use",
                    "RequesterManaged": False,
                },
            ]
        }

        with patch("boto3.client", return_value=mock_ec2):
            summary = manager._summarize_orphaned_enis("gco-us-east-1")

        assert summary["global_accelerator"] == 1
        assert summary["elb"] == 1
        assert summary["eks"] == 1
        assert summary["other"] == 1
        assert summary["vpcs"] == 1
        assert summary["deleted"] == 1
        # Only the detached, non-service-managed ENI is deleted.
        mock_ec2.delete_network_interface.assert_called_once_with(NetworkInterfaceId="eni-eks")

    def test_scopes_describe_to_stack_vpc_and_region(self):
        manager = self._manager()
        mock_ec2 = MagicMock()
        mock_ec2.describe_vpcs.return_value = {"Vpcs": []}

        with patch("boto3.client", return_value=mock_ec2) as mock_client:
            manager._summarize_orphaned_enis("gco-eu-west-1")

        mock_client.assert_called_once_with("ec2", region_name="eu-west-1")
        mock_ec2.describe_vpcs.assert_called_once_with(
            Filters=[{"Name": "tag:aws:cloudformation:stack-name", "Values": ["gco-eu-west-1"]}]
        )

    def test_no_vpcs_returns_zeroed_summary(self):
        manager = self._manager()
        mock_ec2 = MagicMock()
        mock_ec2.describe_vpcs.return_value = {"Vpcs": []}

        with patch("boto3.client", return_value=mock_ec2):
            summary = manager._summarize_orphaned_enis("gco-us-east-1")

        assert summary == {
            "global_accelerator": 0,
            "elb": 0,
            "eks": 0,
            "other": 0,
            "deleted": 0,
            "vpcs": 0,
        }
        mock_ec2.describe_network_interfaces.assert_not_called()

    def test_aws_error_degrades_to_zeroed_summary(self):
        manager = self._manager()
        with patch("boto3.client", side_effect=Exception("no creds")):
            summary = manager._summarize_orphaned_enis("gco-us-east-1")
        assert summary["vpcs"] == 0
        assert summary["deleted"] == 0

    def test_delete_failure_is_swallowed(self):
        """A delete that raises (e.g. still-attaching) must not abort the sweep
        or inflate the deleted count."""
        manager = self._manager()
        mock_ec2 = MagicMock()
        mock_ec2.describe_vpcs.return_value = {"Vpcs": [{"VpcId": "vpc-1"}]}
        mock_ec2.describe_network_interfaces.return_value = {
            "NetworkInterfaces": [
                {
                    "NetworkInterfaceId": "eni-eks",
                    "Description": "aws-K8S-i-0abc",
                    "Status": "available",
                    "RequesterManaged": False,
                },
            ]
        }
        mock_ec2.delete_network_interface.side_effect = Exception("still in use")

        with patch("boto3.client", return_value=mock_ec2):
            summary = manager._summarize_orphaned_enis("gco-us-east-1")

        assert summary["eks"] == 1
        assert summary["deleted"] == 0


class TestPrintOrphanedEniSummary:
    """StackManager._print_orphaned_eni_summary renders a friendly report."""

    def _summary(self, **over):
        base = {
            "global_accelerator": 0,
            "elb": 0,
            "eks": 0,
            "other": 0,
            "deleted": 0,
            "vpcs": 0,
        }
        base.update(over)
        return base

    def test_silent_when_nothing_lingering(self, capsys):
        from cli.stacks import StackManager

        StackManager._print_orphaned_eni_summary("gco-us-east-1", self._summary())
        assert capsys.readouterr().out == ""

    def test_reports_breakdown_and_remaining(self, capsys):
        from cli.stacks import StackManager

        StackManager._print_orphaned_eni_summary(
            "gco-us-east-1",
            self._summary(global_accelerator=2, elb=1, eks=1, deleted=1),
        )
        out = capsys.readouterr().out
        assert "gco-us-east-1" in out
        assert "4 network interface" in out
        assert "2 Global Accelerator-managed" in out
        assert "1 ELB-managed" in out
        assert "Removed 1 detached" in out
        # 4 total - 1 deleted = 3 still held by AWS.
        assert "3 still held by AWS" in out

    def test_no_remaining_line_when_all_cleared(self, capsys):
        from cli.stacks import StackManager

        StackManager._print_orphaned_eni_summary("gco-us-east-1", self._summary(eks=2, deleted=2))
        out = capsys.readouterr().out
        assert "Removed 2 detached" in out
        assert "still held by AWS" not in out


class TestCleanupOrphanedNetworkInterfaces:
    """The public sweep iterates regional stacks only and combines SG + ENI cleanup."""

    def test_iterates_regional_stacks_and_runs_both_passes(self):
        from cli.stacks import StackManager

        config = MagicMock()
        config.project_name = "gco"

        with (
            patch.object(
                StackManager,
                "list_stacks",
                return_value=[
                    "gco-global",
                    "gco-api-gateway",
                    "gco-monitoring",
                    "gco-us-east-1",
                    "gco-eu-west-1",
                ],
            ),
            patch.object(StackManager, "_cleanup_eks_security_groups") as mock_sg,
            patch.object(
                StackManager, "_summarize_orphaned_enis", return_value={}
            ) as mock_summarize,
            patch.object(StackManager, "_print_orphaned_eni_summary") as mock_print,
            patch.object(StackManager, "_find_project_root", return_value=Path(".")),
        ):
            manager = StackManager(config)
            manager.cleanup_orphaned_network_interfaces()

        # Only the two regional stacks get cleaned — not global/api-gateway/monitoring.
        assert mock_sg.call_count == 2
        assert mock_summarize.call_count == 2
        assert mock_print.call_count == 2
        for stack in ("gco-us-east-1", "gco-eu-west-1"):
            mock_sg.assert_any_call(stack)
            mock_summarize.assert_any_call(stack)
