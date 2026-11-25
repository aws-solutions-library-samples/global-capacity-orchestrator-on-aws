"""Tests for the destroy/deploy hardening in cli/stacks.py.

Two behaviours land here:

1. State-aware deploy verification — ``deploy()`` no longer trusts cdk's exit
   code alone on success; it confirms CloudFormation actually shows a terminal
   ``CREATE_COMPLETE`` / ``UPDATE_COMPLETE`` so a silent rollback can't read as a
   successful deploy. An unknown status (``None``) leaves cdk's verdict intact.
   The inverse also holds: when cdk exits non-zero but CloudFormation is still
   mid-operation (e.g. cdk died on a transient ``read EADDRNOTAVAIL`` socket
   error), ``deploy()`` waits for the stack to settle via
   ``_wait_for_stack_settle`` and treats a terminal ``CREATE_COMPLETE`` /
   ``UPDATE_COMPLETE`` as success rather than a false failure.

2. Orphaned-ENI sweep — the between-retry cleanup is generalized from "EKS
   cluster SG + its ENIs" to a report-and-clear pass over every network
   interface still in a regional stack's VPC, categorized (Global Accelerator /
   ELB / EKS / other). Detached, non-service-managed interfaces are deleted;
   service-managed ones (released asynchronously by AWS) are reported, not
   fought.
"""

from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError


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


class TestDeployFailureReconcileWaitsForSettle:
    """When cdk dies on a transient client error (e.g. read EADDRNOTAVAIL) while
    CloudFormation is still mid-flight, deploy() waits for the stack to settle to
    a terminal state and judges that — not a status read taken the instant cdk
    exits, which can catch the stack seconds before CREATE_COMPLETE."""

    def _deploy(self, status_sequence, *, returncode=1, stack="gco-monitoring"):
        from cli.stacks import StackManager

        config = MagicMock()
        config.global_region = "us-east-2"
        get_status = MagicMock(side_effect=list(status_sequence))
        with (
            patch("cli.stacks._detect_container_runtime", return_value="docker"),
            patch.object(StackManager, "_check_and_fix_stuck_stack"),
            patch.object(StackManager, "ensure_bootstrapped", return_value=True),
            patch.object(StackManager, "_run_cdk") as mock_run,
            patch.object(StackManager, "_get_stack_status", get_status),
            # Default the last-operation marker to well before the deploy
            # started, modelling a stack sitting in a *prior* deploy's terminal
            # state. The "already terminal" tests rely on this to prove a stale
            # COMPLETE reads as a failure; the in-progress tests settle via
            # _wait_for_stack_settle and never consult it.
            patch.object(
                StackManager,
                "_get_stack_last_update_time",
                return_value=datetime(2000, 1, 1, tzinfo=UTC),
            ),
            patch.object(StackManager, "_diagnose_deploy_failure"),
            patch("time.sleep") as mock_sleep,
        ):
            mock_run.return_value = MagicMock(returncode=returncode)
            manager = StackManager(config)
            result = manager.deploy(stack_name=stack, require_approval=False)
        return result, get_status, mock_sleep

    def test_in_progress_then_complete_is_success(self):
        """cdk fails; CFN reads CREATE_IN_PROGRESS, then settles CREATE_COMPLETE."""
        result, get_status, _ = self._deploy(
            [
                "CREATE_IN_PROGRESS",  # failure-reconcile read
                "CREATE_IN_PROGRESS",  # first settle poll
                "CREATE_COMPLETE",  # settle poll resolves
                "CREATE_COMPLETE",  # success-path re-verification
            ]
        )
        assert result is True
        assert get_status.call_count == 4

    def test_in_progress_then_rollback_is_failure(self):
        """A stack that settles into rollback stays a failed deploy."""
        result, _, _ = self._deploy(
            ["CREATE_IN_PROGRESS", "ROLLBACK_IN_PROGRESS", "ROLLBACK_COMPLETE"]
        )
        assert result is False

    def test_already_complete_without_in_progress_is_failure(self):
        """cdk exits non-zero while the stack is already terminal *_COMPLETE:
        CloudFormation ran no operation for this attempt (cdk failed *before*
        touching it — a synth error, a cloud-assembly schema mismatch, or an
        asset/image build failure). The stale COMPLETE left by a prior deploy
        must NOT be reported as success, and there is nothing to wait on."""
        result, get_status, mock_sleep = self._deploy(["CREATE_COMPLETE"])
        assert result is False
        mock_sleep.assert_not_called()
        assert get_status.call_count == 1

    def test_update_complete_from_prior_deploy_is_not_masked(self):
        """Regression: a cdk build/synth failure (e.g. a cloud-assembly schema
        mismatch, or a missing container runtime during asset build) leaves the
        stack in the UPDATE_COMPLETE state of the *previous* deploy. The wrapper
        must surface the failure instead of reporting the stale terminal state
        as a fresh successful deploy."""
        result, _, mock_sleep = self._deploy(["UPDATE_COMPLETE"])
        assert result is False
        mock_sleep.assert_not_called()

    def test_unknown_status_keeps_cdk_failure(self):
        """A None status (lookup failed / transient) leaves cdk's failure intact."""
        result, _, mock_sleep = self._deploy([None])
        assert result is False
        mock_sleep.assert_not_called()


class TestWaitForStackSettle:
    """_wait_for_stack_settle polls CloudFormation out of *_IN_PROGRESS states."""

    def _manager(self):
        from cli.stacks import StackManager

        config = MagicMock()
        config.global_region = "us-east-2"
        with patch("cli.stacks._detect_container_runtime", return_value="docker"):
            return StackManager(config)

    def test_returns_terminal_immediately_without_sleeping(self):
        from cli.stacks import StackManager

        manager = self._manager()
        with (
            patch.object(StackManager, "_get_stack_status", return_value="CREATE_COMPLETE") as gs,
            patch("time.sleep") as mock_sleep,
        ):
            assert manager._wait_for_stack_settle("gco-monitoring") == "CREATE_COMPLETE"
            gs.assert_called_once()
            mock_sleep.assert_not_called()

    def test_polls_until_terminal(self):
        from cli.stacks import StackManager

        manager = self._manager()
        seq = ["CREATE_IN_PROGRESS", "CREATE_IN_PROGRESS", "CREATE_COMPLETE"]
        with (
            patch.object(StackManager, "_get_stack_status", side_effect=seq),
            patch("time.sleep") as mock_sleep,
        ):
            assert manager._wait_for_stack_settle("gco-monitoring") == "CREATE_COMPLETE"
            assert mock_sleep.call_count == 2

    def test_transient_none_status_retries_until_visible(self):
        from cli.stacks import StackManager

        manager = self._manager()
        with (
            patch.object(
                StackManager,
                "_get_stack_status",
                side_effect=[None, "CREATE_IN_PROGRESS", "CREATE_COMPLETE"],
            ) as status,
            patch("time.sleep") as mock_sleep,
        ):
            assert manager._wait_for_stack_settle("gco-monitoring") == "CREATE_COMPLETE"
            assert status.call_count == 3
            assert [call.args[0] for call in mock_sleep.call_args_list] == [5.0, 15.0]

    def test_persistent_none_status_respects_zero_timeout(self):
        from cli.stacks import StackManager

        manager = self._manager()
        with (
            patch.object(StackManager, "_get_stack_status", return_value=None) as status,
            patch("time.sleep") as mock_sleep,
        ):
            assert manager._wait_for_stack_settle("gco-monitoring", timeout=0) is None
            status.assert_called_once()
            mock_sleep.assert_not_called()

    def test_persistent_none_status_is_bounded_to_sixty_seconds(self):
        from cli.stacks import StackManager

        manager = self._manager()
        clock = {"now": 0.0}

        def advance(seconds):
            clock["now"] += float(seconds)

        with (
            patch.object(StackManager, "_get_stack_status", return_value=None) as status,
            patch("time.monotonic", side_effect=lambda: clock["now"]),
            patch("time.sleep", side_effect=advance) as sleep,
        ):
            assert manager._wait_for_stack_settle("gco-monitoring", timeout=120) is None

        assert clock["now"] == 60.0
        assert status.call_count == 13
        assert [call.args[0] for call in sleep.call_args_list] == [5.0] * 12

    def test_unknown_status_never_outlives_shorter_overall_deadline(self):
        from cli.stacks import StackManager

        manager = self._manager()
        clock = {"now": 0.0}

        def advance(seconds):
            clock["now"] += float(seconds)

        with (
            patch.object(StackManager, "_get_stack_status", return_value=None),
            patch("time.monotonic", side_effect=lambda: clock["now"]),
            patch("time.sleep", side_effect=advance) as sleep,
        ):
            assert manager._wait_for_stack_settle("gco-monitoring", timeout=12) is None

        assert clock["now"] == 12.0
        assert [call.args[0] for call in sleep.call_args_list] == [5.0, 5.0, 2.0]

    def test_known_status_resets_a_later_unknown_window(self):
        from cli.stacks import StackManager

        manager = self._manager()
        clock = {"now": 0.0}
        statuses = [None, "CREATE_IN_PROGRESS", None, "CREATE_COMPLETE"]

        def advance(seconds):
            clock["now"] += float(seconds)

        with (
            patch.object(StackManager, "_get_stack_status", side_effect=statuses),
            patch("time.monotonic", side_effect=lambda: clock["now"]),
            patch("time.sleep", side_effect=advance) as sleep,
        ):
            assert manager._wait_for_stack_settle("gco-monitoring", timeout=120) == (
                "CREATE_COMPLETE"
            )

        assert [call.args[0] for call in sleep.call_args_list] == [5.0, 15.0, 5.0]

    def test_cancellation_during_unknown_status_does_not_retry(self):
        from cli.stacks import StackManager

        manager = self._manager()
        manager._cdk_cancel_event.set()
        with (
            patch.object(StackManager, "_get_stack_status", return_value=None) as status,
            patch("time.sleep") as sleep,
        ):
            assert manager._wait_for_stack_settle("gco-monitoring", timeout=120) is None

        status.assert_called_once()
        sleep.assert_not_called()

    def test_timeout_gives_up_and_returns_last_status(self):
        from cli.stacks import StackManager

        manager = self._manager()
        with (
            patch.object(StackManager, "_get_stack_status", return_value="CREATE_IN_PROGRESS"),
            patch("time.sleep") as mock_sleep,
        ):
            # timeout=0 → deadline already passed → bail after the first read.
            result = manager._wait_for_stack_settle("gco-monitoring", timeout=0)
            assert result == "CREATE_IN_PROGRESS"
            mock_sleep.assert_not_called()


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

    def test_a_stack_naming_no_region_is_inspected_only_in_a_given_region(self):
        manager = self._manager()
        mock_ec2 = MagicMock()
        mock_ec2.describe_vpcs.return_value = {"Vpcs": []}

        with patch("boto3.client", return_value=mock_ec2) as mock_client:
            assert manager._summarize_orphaned_enis("gco-analytics")["vpcs"] == 0
            mock_client.assert_not_called()
            manager._summarize_orphaned_enis("gco-analytics", region="us-east-2")

        mock_client.assert_called_once_with("ec2", region_name="us-east-2")
        mock_ec2.describe_vpcs.assert_called_once_with(
            Filters=[{"Name": "tag:aws:cloudformation:stack-name", "Values": ["gco-analytics"]}]
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

    @pytest.mark.parametrize("keep_control_plane", [False, True])
    def test_analytics_vpc_is_swept_in_its_own_region_and_bridges_are_skipped(
        self, keep_control_plane
    ):
        """No sweep runs against a pseudo-Region (``analytics``, ``regional-api-…``).

        The analytics stack owns a VPC in the API Gateway Region but no EKS
        cluster: a full teardown sweeps only its ENIs there, and a
        keep-control-plane teardown, which keeps it standing, leaves it alone.
        """
        from unittest.mock import call

        from cli.stacks import StackManager

        config = MagicMock()
        config.project_name = "gco"

        with (
            patch.object(
                StackManager,
                "list_stacks",
                return_value=[
                    "gco-global",
                    "gco-analytics",
                    "gco-us-east-1",
                    "gco-regional-api-us-east-1",
                ],
            ),
            patch.object(StackManager, "_get_deploy_region", return_value="us-east-2"),
            patch.object(StackManager, "_cleanup_eks_security_groups") as mock_sg,
            patch.object(
                StackManager, "_summarize_orphaned_enis", return_value={}
            ) as mock_summarize,
            patch.object(StackManager, "_print_orphaned_eni_summary") as mock_print,
            patch.object(StackManager, "_find_project_root", return_value=Path(".")),
        ):
            StackManager(config).cleanup_orphaned_network_interfaces(
                keep_control_plane=keep_control_plane
            )

        mock_sg.assert_called_once_with("gco-us-east-1")
        expected = [call("gco-us-east-1")]
        if not keep_control_plane:
            expected.insert(0, call("gco-analytics", region="us-east-2"))
        assert mock_summarize.call_args_list == expected
        assert mock_print.call_count == len(expected)


class TestCleanupOrphanedBastions:
    """Destroy-all removes CLI-managed bastions before CloudFormation runs."""

    @staticmethod
    def _manager():
        from cli.stacks import StackManager

        manager = StackManager.__new__(StackManager)
        manager.config = MagicMock(project_name="gco")
        manager.project_root = Path(".")
        return manager

    @staticmethod
    def _tags(**overrides):
        tags = {
            "gco:ephemeral": "true",
            "gco:purpose": "cluster-observability",
            "gco:project": "gco",
            "Name": "gco-ephemeral-ssm-bastion",
        }
        tags.update(overrides)
        return [{"Key": key, "Value": value} for key, value in tags.items()]

    def test_terminates_current_and_legacy_project_bastions(self):
        from cli.stacks import StackManager

        manager = self._manager()
        mock_ec2 = MagicMock()
        mock_ec2.describe_vpcs.return_value = {"Vpcs": [{"VpcId": "vpc-1"}]}
        legacy_tags = [tag for tag in self._tags() if tag["Key"] != "gco:project"]
        mock_ec2.describe_instances.return_value = {
            "Reservations": [
                {
                    "Instances": [
                        {
                            "InstanceId": "i-current",
                            "Tags": self._tags(),
                            "NetworkInterfaces": [
                                {
                                    "NetworkInterfaceId": "eni-current",
                                    "Attachment": {
                                        "DeviceIndex": 0,
                                        "DeleteOnTermination": True,
                                    },
                                },
                                {
                                    "NetworkInterfaceId": "eni-secondary",
                                    "Attachment": {
                                        "DeviceIndex": 1,
                                        "DeleteOnTermination": False,
                                    },
                                },
                            ],
                        },
                        {
                            "InstanceId": "i-legacy",
                            "Tags": legacy_tags,
                            "NetworkInterfaces": [
                                {
                                    "NetworkInterfaceId": "eni-legacy",
                                    "Attachment": {
                                        "DeviceIndex": 0,
                                        "DeleteOnTermination": True,
                                    },
                                }
                            ],
                        },
                        {
                            "InstanceId": "i-other-project",
                            "Tags": self._tags(**{"gco:project": "other"}),
                            "NetworkInterfaces": [
                                {
                                    "NetworkInterfaceId": "eni-other",
                                    "Attachment": {
                                        "DeviceIndex": 0,
                                        "DeleteOnTermination": True,
                                    },
                                }
                            ],
                        },
                    ]
                }
            ]
        }

        with (
            patch("boto3.client", return_value=mock_ec2),
            patch.object(
                StackManager, "_wait_for_bastion_network_interfaces", return_value=set()
            ) as wait_for_enis,
        ):
            count = manager._cleanup_orphaned_bastions("gco-us-east-1")

        assert count == 2
        mock_ec2.terminate_instances.assert_called_once_with(InstanceIds=["i-current", "i-legacy"])
        mock_ec2.get_waiter.return_value.wait.assert_called_once_with(
            InstanceIds=["i-current", "i-legacy"],
            WaiterConfig={"Delay": 5, "MaxAttempts": 60},
        )
        wait_for_enis.assert_called_once_with(mock_ec2, ["eni-current", "eni-legacy"])
        filters = mock_ec2.describe_instances.call_args.kwargs["Filters"]
        assert {"Name": "vpc-id", "Values": ["vpc-1"]} in filters
        assert {"Name": "tag:gco:ephemeral", "Values": ["true"]} in filters

    def test_no_stack_vpc_is_a_noop(self):
        manager = self._manager()
        mock_ec2 = MagicMock()
        mock_ec2.describe_vpcs.return_value = {"Vpcs": []}

        with patch("boto3.client", return_value=mock_ec2):
            assert manager._cleanup_orphaned_bastions("gco-us-east-1") == 0

        mock_ec2.describe_instances.assert_not_called()
        mock_ec2.terminate_instances.assert_not_called()

    def test_public_sweep_only_visits_regional_stacks(self):
        from cli.stacks import StackManager

        manager = self._manager()
        stacks = [
            "gco-global",
            "gco-api-gateway",
            "gco-monitoring",
            "gco-analytics",
            "gco-us-east-1",
            "gco-eu-west-1",
        ]
        with patch.object(StackManager, "_cleanup_orphaned_bastions", return_value=1) as cleanup:
            assert manager.cleanup_orphaned_bastions(stacks) == 2

        assert cleanup.call_count == 2
        cleanup.assert_any_call(
            "gco-us-east-1",
            region=None,
            vpc_id=None,
            fail_closed=False,
        )
        cleanup.assert_any_call(
            "gco-eu-west-1",
            region=None,
            vpc_id=None,
            fail_closed=False,
        )

    def test_public_sweep_is_silent_when_nothing_was_terminated(self, capsys):
        """The usual case: no stray bastion, so no termination line is printed."""
        from cli.stacks import StackManager

        manager = self._manager()
        with patch.object(StackManager, "_cleanup_orphaned_bastions", return_value=0) as cleanup:
            assert manager.cleanup_orphaned_bastions(["gco-global", "gco-us-east-1"]) == 0
        cleanup.assert_called_once()
        assert "Requested termination" not in capsys.readouterr().out


class TestWaitForBastionNetworkInterfaces:
    """Bastion ENI release waits for EC2 and clears detached leftovers."""

    def test_deletes_available_interface(self):
        from cli.stacks import StackManager

        ec2 = MagicMock()
        ec2.describe_network_interfaces.return_value = {
            "NetworkInterfaces": [{"NetworkInterfaceId": "eni-1", "Status": "available"}]
        }

        remaining = StackManager._wait_for_bastion_network_interfaces(
            ec2, ["eni-1"], timeout_seconds=0
        )

        assert remaining == set()
        ec2.delete_network_interface.assert_called_once_with(NetworkInterfaceId="eni-1")

    def test_not_found_means_interface_is_released(self):
        from cli.stacks import StackManager

        ec2 = MagicMock()
        ec2.describe_network_interfaces.side_effect = ClientError(
            {"Error": {"Code": "InvalidNetworkInterfaceID.NotFound", "Message": "gone"}},
            "DescribeNetworkInterfaces",
        )

        remaining = StackManager._wait_for_bastion_network_interfaces(
            ec2, ["eni-gone"], timeout_seconds=0
        )

        assert remaining == set()

    def test_in_use_interface_remains_at_timeout(self):
        from cli.stacks import StackManager

        ec2 = MagicMock()
        ec2.describe_network_interfaces.return_value = {
            "NetworkInterfaces": [{"NetworkInterfaceId": "eni-1", "Status": "in-use"}]
        }

        remaining = StackManager._wait_for_bastion_network_interfaces(
            ec2, ["eni-1"], timeout_seconds=0
        )

        assert remaining == {"eni-1"}
        ec2.delete_network_interface.assert_not_called()


class TestImplicitLogGroupCleanup:
    """Non-strict teardown sweeps the log groups CloudFormation never modeled.

    Lambda default groups and the EKS control-plane/Container Insights
    groups are created out-of-band by the services, so ``destroy-all``
    used to report success while orphaning them (22 survived a real
    teardown and failed the live-validation clean-account gate). The
    sweep may only ever delete exact names derived from the project's
    own stack resources, and only for stacks whose deletion succeeded.
    """

    def _manager(self):
        from cli.stacks import StackManager

        config = MagicMock()
        config.project_name = "gco"
        return StackManager(config)

    # -- name derivation ----------------------------------------------

    def test_lambda_function_derives_its_default_group(self):
        from cli.stacks import StackManager

        names = StackManager._implicit_log_group_names(
            "AWS::Lambda::Function", "gco-us-east-1-GaRegistration-UJUi"
        )
        assert names == ("/aws/lambda/gco-us-east-1-GaRegistration-UJUi",)

    def test_eks_cluster_derives_control_plane_and_container_insights(self):
        from cli.stacks import StackManager

        names = StackManager._implicit_log_group_names("AWS::EKS::Cluster", "gco-us-east-1")
        assert names == (
            "/aws/eks/gco-us-east-1/cluster",
            "/aws/containerinsights/gco-us-east-1/application",
            "/aws/containerinsights/gco-us-east-1/dataplane",
            "/aws/containerinsights/gco-us-east-1/host",
            "/aws/containerinsights/gco-us-east-1/performance",
        )

    def test_explicit_log_group_resources_are_cloudformation_owned(self):
        """CFN deletes modeled AWS::Logs::LogGroup resources itself."""
        from cli.stacks import StackManager

        assert StackManager._implicit_log_group_names("AWS::Logs::LogGroup", "/gco/api") == ()
        assert StackManager._implicit_log_group_names("AWS::SQS::Queue", "gco-jobs") == ()

    def test_hyperpod_cluster_derives_its_node_log_group_from_arn_and_name(self):
        """``/aws/sagemaker/Clusters/<name>/<id>`` needs the name the ARN lacks.

        HyperPod creates the group with the cluster and never deletes it; the
        fourth #502 release attempt's final inventory found it after the stack
        was gone.
        """
        from cli.stacks import StackManager

        arn = "arn:aws:sagemaker:us-east-1:111111111111:cluster/2b6s56ajc992"
        assert StackManager._implicit_log_group_names(
            "AWS::SageMaker::Cluster", arn, "gco-hyperpod"
        ) == ("/aws/sagemaker/Clusters/gco-hyperpod/2b6s56ajc992",)
        assert StackManager._implicit_log_group_names("AWS::SageMaker::Cluster", arn) == ()
        assert StackManager._implicit_log_group_names("AWS::SageMaker::Cluster", arn, "") == ()
        assert StackManager._implicit_log_group_names("AWS::SageMaker::Cluster", arn, "a/b") == ()
        assert (
            StackManager._implicit_log_group_names(
                "AWS::SageMaker::Cluster", "gco-hyperpod", "gco-hyperpod"
            )
            == ()
        )
        assert StackManager._implicit_log_group_names(
            "AWS::EKS::Cluster", "gco-us-east-1", "x"
        ) == StackManager._implicit_log_group_names("AWS::EKS::Cluster", "gco-us-east-1")

    def test_hyperpod_cluster_name_comes_from_the_service_and_degrades_to_none(self):
        from cli.stacks import StackManager

        arn = "arn:aws:sagemaker:us-east-1:111111111111:cluster/2b6s56ajc992"
        sagemaker = MagicMock()
        sagemaker.describe_cluster.return_value = {"ClusterName": "gco-hyperpod", "ClusterArn": arn}
        with patch("boto3.client", return_value=sagemaker) as client:
            assert StackManager._hyperpod_cluster_name("us-east-1", arn) == "gco-hyperpod"
        client.assert_called_once_with("sagemaker", region_name="us-east-1")
        sagemaker.describe_cluster.assert_called_once_with(ClusterName=arn)

        sagemaker.describe_cluster.return_value = {"ClusterArn": arn}
        with patch("boto3.client", return_value=sagemaker):
            assert StackManager._hyperpod_cluster_name("us-east-1", arn) is None

        sagemaker.describe_cluster.side_effect = RuntimeError("ResourceNotFound")
        with patch("boto3.client", return_value=sagemaker):
            assert StackManager._hyperpod_cluster_name("us-east-1", arn) is None

    # -- collection ----------------------------------------------------

    def _stack_target(self, resources):
        cfn = MagicMock()
        paginator = MagicMock()
        paginator.paginate.return_value = [{"StackResourceSummaries": resources}]
        cfn.get_paginator.return_value = paginator
        stack = {"StackId": "arn:aws:cloudformation:us-east-1:111111111111:stack/gco-us-east-1/x"}
        return ("us-east-1", cfn, stack)

    def test_collects_exact_names_from_live_stack_resources(self):
        manager = self._manager()
        cluster_arn = "arn:aws:sagemaker:us-east-1:111111111111:cluster/2b6s56ajc992"
        target = self._stack_target(
            [
                {
                    "ResourceType": "AWS::Lambda::Function",
                    "PhysicalResourceId": "gco-helm-us-east-1",
                },
                {"ResourceType": "AWS::EKS::Cluster", "PhysicalResourceId": "gco-us-east-1"},
                {"ResourceType": "AWS::Logs::LogGroup", "PhysicalResourceId": "/gco/explicit"},
                {"ResourceType": "AWS::Lambda::Function", "PhysicalResourceId": ""},
                {"ResourceType": "AWS::SageMaker::Cluster", "PhysicalResourceId": cluster_arn},
            ]
        )
        with (
            patch.object(manager, "_describe_stack_target", return_value=target),
            patch.object(
                manager, "_hyperpod_cluster_name", return_value="gco-hyperpod"
            ) as cluster_name,
        ):
            collected = manager._collect_implicit_log_groups(["gco-us-east-1"])

        # The HyperPod cluster's name is asked of the service exactly once,
        # for the cluster resource alone.
        cluster_name.assert_called_once_with("us-east-1", cluster_arn)
        assert collected == {
            "gco-us-east-1": {
                "region": "us-east-1",
                "log_groups": [
                    "/aws/containerinsights/gco-us-east-1/application",
                    "/aws/containerinsights/gco-us-east-1/dataplane",
                    "/aws/containerinsights/gco-us-east-1/host",
                    "/aws/containerinsights/gco-us-east-1/performance",
                    "/aws/eks/gco-us-east-1/cluster",
                    "/aws/lambda/gco-helm-us-east-1",
                    "/aws/sagemaker/Clusters/gco-hyperpod/2b6s56ajc992",
                ],
            }
        }

    def test_absent_stack_collects_nothing(self):
        manager = self._manager()
        with patch.object(manager, "_describe_stack_target", return_value=None):
            assert manager._collect_implicit_log_groups(["gco-us-east-1"]) == {}

    def test_stack_with_no_implicit_log_groups_is_omitted(self):
        """A live stack made up only of explicitly-modeled resources (a
        CFN-owned LogGroup, an SQS queue) contributes no entry at all —
        it is not present with an empty list."""
        manager = self._manager()
        target = self._stack_target(
            [
                {"ResourceType": "AWS::Logs::LogGroup", "PhysicalResourceId": "/gco/explicit"},
                {"ResourceType": "AWS::SQS::Queue", "PhysicalResourceId": "gco-jobs"},
            ]
        )
        with patch.object(manager, "_describe_stack_target", return_value=target):
            assert manager._collect_implicit_log_groups(["gco-us-east-1"]) == {}

    def test_collection_is_best_effort_per_stack(self):
        """One stack's describe failure must not block the others."""
        manager = self._manager()
        good = self._stack_target(
            [{"ResourceType": "AWS::Lambda::Function", "PhysicalResourceId": "gco-fn"}]
        )

        def describe(stack_name):
            if stack_name == "gco-broken":
                raise RuntimeError("CloudFormation unavailable")
            return good

        with patch.object(manager, "_describe_stack_target", side_effect=describe):
            collected = manager._collect_implicit_log_groups(["gco-broken", "gco-us-east-1"])

        assert list(collected) == ["gco-us-east-1"]

    # -- deletion ------------------------------------------------------

    def _collected(self):
        return {
            "gco-us-east-1": {
                "region": "us-east-1",
                "log_groups": ["/aws/eks/gco-us-east-1/cluster", "/aws/lambda/gco-fn"],
            },
            "gco-global": {
                "region": "us-east-2",
                "log_groups": ["/aws/lambda/gco-global-Poller"],
            },
        }

    def test_deletes_only_groups_of_successfully_destroyed_stacks(self):
        manager = self._manager()
        logs = MagicMock()
        with patch("boto3.client", return_value=logs) as client_factory:
            outcome = manager._cleanup_implicit_log_groups(self._collected(), ["gco-us-east-1"])

        client_factory.assert_called_once_with("logs", region_name="us-east-1")
        deleted = {call.kwargs["logGroupName"] for call in logs.delete_log_group.call_args_list}
        assert deleted == {"/aws/eks/gco-us-east-1/cluster", "/aws/lambda/gco-fn"}
        assert outcome["deleted"] == [
            "us-east-1:/aws/eks/gco-us-east-1/cluster",
            "us-east-1:/aws/lambda/gco-fn",
        ]
        assert outcome["errors"] == []

    def test_missing_group_is_recorded_not_retried(self):
        """A Lambda that never logged has no default group — that's normal."""
        manager = self._manager()
        logs = MagicMock()
        logs.delete_log_group.side_effect = ClientError(
            {"Error": {"Code": "ResourceNotFoundException"}}, "DeleteLogGroup"
        )
        with patch("boto3.client", return_value=logs):
            outcome = manager._cleanup_implicit_log_groups(
                self._collected(), ["gco-us-east-1", "gco-global"]
            )

        assert outcome["deleted"] == []
        assert len(outcome["missing"]) == 3
        assert outcome["errors"] == []

    def test_delete_errors_are_recorded_and_swallowed(self):
        """Cleanup must never convert a successful destroy into a failure."""
        manager = self._manager()
        logs = MagicMock()
        logs.delete_log_group.side_effect = ClientError(
            {"Error": {"Code": "AccessDeniedException"}}, "DeleteLogGroup"
        )
        with patch("boto3.client", return_value=logs):
            outcome = manager._cleanup_implicit_log_groups(self._collected(), ["gco-global"])

        assert outcome["errors"] == [
            "us-east-2:/aws/lambda/gco-global-Poller: AccessDeniedException"
        ]


class TestRegeneratedLogGroupCleanup:
    """Non-strict teardown deletes explicit log groups Lambda recreated.

    A provider-framework Lambda's last log lines can land seconds after
    CloudFormation deleted the function's explicit log group, and Lambda then
    creates the group again, owned by no stack. Four such groups survived a
    two-Region ``destroy-all`` and blocked the next live-validation baseline.
    """

    _STACK_ID = "arn:aws:cloudformation:us-east-1:111111111111:stack/gco-us-east-1/x"

    def _manager(self):
        from cli.stacks import StackManager

        config = MagicMock()
        config.project_name = "gco"
        return StackManager(config)

    # -- collection ----------------------------------------------------

    def test_collects_the_region_and_id_of_each_live_stack(self):
        manager = self._manager()
        target = ("us-east-1", MagicMock(), {"StackId": self._STACK_ID})

        def describe(stack_name):
            return None if stack_name == "gco-gone" else target

        with patch.object(manager, "_describe_stack_target", side_effect=describe):
            identities = manager._collect_stack_identities(["gco-us-east-1", "gco-gone"])

        assert identities == {"gco-us-east-1": {"region": "us-east-1", "stack_id": self._STACK_ID}}

    def test_collection_is_best_effort_per_stack(self, caplog):
        manager = self._manager()
        target = ("us-east-1", MagicMock(), {"StackId": self._STACK_ID})

        def describe(stack_name):
            if stack_name == "gco-broken":
                raise RuntimeError("CloudFormation unavailable")
            return target

        with patch.object(manager, "_describe_stack_target", side_effect=describe):
            identities = manager._collect_stack_identities(["gco-broken", "gco-us-east-1"])

        assert list(identities) == ["gco-us-east-1"]
        assert "Could not read the stack ID of gco-broken" in caplog.text

    # -- deletion ------------------------------------------------------

    @staticmethod
    def _cloudformation(resources=(), error=None):
        cloudformation = MagicMock()
        paginator = cloudformation.get_paginator.return_value
        if error is not None:
            paginator.paginate.side_effect = error
        else:
            paginator.paginate.return_value = [{"StackResourceSummaries": list(resources)}]
        return cloudformation

    @staticmethod
    def _log_group(name, status="DELETE_COMPLETE"):
        return {
            "ResourceType": "AWS::Logs::LogGroup",
            "ResourceStatus": status,
            "PhysicalResourceId": name,
        }

    def test_deletes_only_groups_cloudformation_already_deleted(self, capsys):
        """A retained group, another resource type, or a blank ID is never touched."""
        manager = self._manager()
        cloudformation = self._cloudformation(
            [
                self._log_group("gco-us-east-1-GaDeregistrationProviderLogGroup-abc"),
                self._log_group("gco-us-east-1-HelmTeardownProviderLogGroup-def"),
                self._log_group("gco-us-east-1-RetainedLogGroup-ghi", status="DELETE_SKIPPED"),
                self._log_group(""),
                {
                    "ResourceType": "AWS::SQS::Queue",
                    "ResourceStatus": "DELETE_COMPLETE",
                    "PhysicalResourceId": "gco-jobs",
                },
            ]
        )
        logs = MagicMock()
        logs.delete_log_group.side_effect = [
            None,
            ClientError({"Error": {"Code": "ResourceNotFoundException"}}, "DeleteLogGroup"),
        ]
        clients = {("cloudformation", "us-east-1"): cloudformation, ("logs", "us-east-1"): logs}
        identities = {
            "gco-us-east-1": {"region": "us-east-1", "stack_id": self._STACK_ID},
            "gco-us-west-2": {"region": "us-west-2", "stack_id": "still-failing"},
        }

        with patch(
            "boto3.client", side_effect=lambda service, region_name: clients[(service, region_name)]
        ):
            # gco-global deleted but has no recorded identity; gco-us-west-2
            # has one but did not delete.
            outcome = manager._cleanup_regenerated_log_groups(
                identities, ["gco-us-east-1", "gco-global"]
            )

        # A deleted stack is read by its ID; its name no longer resolves.
        cloudformation.get_paginator.assert_called_once_with("list_stack_resources")
        cloudformation.get_paginator.return_value.paginate.assert_called_once_with(
            StackName=self._STACK_ID
        )
        assert [call.kwargs for call in logs.delete_log_group.call_args_list] == [
            {"logGroupName": "gco-us-east-1-GaDeregistrationProviderLogGroup-abc"},
            {"logGroupName": "gco-us-east-1-HelmTeardownProviderLogGroup-def"},
        ]
        # A group that did not come back is the normal case, not an outcome.
        assert outcome == {
            "deleted": ["us-east-1:gco-us-east-1-GaDeregistrationProviderLogGroup-abc"],
            "errors": [],
        }
        assert "Deleted 1 CloudWatch log group(s) that Lambda recreated" in capsys.readouterr().out

    def test_errors_are_recorded_and_swallowed(self, caplog, capsys):
        """Cleanup must never convert a successful destroy into a failure."""
        manager = self._manager()
        logs = MagicMock()
        logs.delete_log_group.side_effect = [
            ClientError({"Error": {"Code": "AccessDeniedException"}}, "DeleteLogGroup"),
            RuntimeError("connection reset"),
        ]
        clients = {
            ("cloudformation", "us-east-1"): self._cloudformation(
                [self._log_group("gco-a"), self._log_group("gco-b")]
            ),
            ("logs", "us-east-1"): logs,
            ("cloudformation", "us-west-2"): self._cloudformation(
                error=RuntimeError("stack listing failed")
            ),
        }
        identities = {
            "gco-us-east-1": {"region": "us-east-1", "stack_id": self._STACK_ID},
            "gco-us-west-2": {"region": "us-west-2", "stack_id": "unreadable"},
        }

        with patch(
            "boto3.client", side_effect=lambda service, region_name: clients[(service, region_name)]
        ):
            outcome = manager._cleanup_regenerated_log_groups(
                identities, ["gco-us-east-1", "gco-us-west-2"]
            )

        assert outcome == {
            "deleted": [],
            "errors": [
                "us-east-1:gco-a: AccessDeniedException",
                "us-east-1:gco-b: RuntimeError: connection reset",
                "us-west-2:gco-us-west-2: RuntimeError: stack listing failed",
            ],
        }
        assert "Regenerated log-group cleanup failed for us-east-1:gco-a" in caplog.text
        assert "Deleted" not in capsys.readouterr().out


class TestRemovedFunctionLogGroups:
    """The log groups of the functions an in-place update removed from a stack.

    A keep-control-plane teardown updates the monitoring stack so it stops
    referencing the regional stacks, which removes its cross-region export
    reader. CloudFormation deleted the reader's Lambda function, but a
    ``gco upgrade`` left the function's ``/aws/lambda/<function>`` group behind.
    """

    _STACK_ID = "arn:aws:cloudformation:us-east-2:111111111111:stack/gco-monitoring/x"
    _READER = "gco-monitoring-CustomCrossRegionExportReaderCustom-abc123"
    _AUTO_DELETE = "gco-monitoring-CustomS3AutoDeleteObjectsCustomReso-def456"

    def _manager(self):
        from cli.stacks import StackManager

        config = MagicMock()
        config.project_name = "gco"
        return StackManager(config)

    @staticmethod
    def _cloudformation(*pages, error=None):
        cloudformation = MagicMock()
        paginator = cloudformation.get_paginator.return_value
        if error is not None:
            paginator.paginate.side_effect = error
        else:
            paginator.paginate.return_value = [
                {"StackResourceSummaries": list(page)} for page in pages
            ]
        return cloudformation

    @staticmethod
    def _resource(physical_id, resource_type="AWS::Lambda::Function"):
        return {"ResourceType": resource_type, "PhysicalResourceId": physical_id}

    def _before(self):
        return {
            "region": "us-east-2",
            "stack_id": self._STACK_ID,
            "functions": [self._READER, self._AUTO_DELETE],
        }

    # -- listing -------------------------------------------------------

    def test_lists_the_function_names_of_a_live_stack_across_pages(self):
        """Other resource types and a blank physical ID are not functions."""
        manager = self._manager()
        cloudformation = self._cloudformation(
            [self._resource(self._READER), self._resource("gco-cost-reports", "AWS::S3::Bucket")],
            [self._resource(self._AUTO_DELETE), self._resource("")],
        )
        target = ("us-east-2", cloudformation, {"StackId": self._STACK_ID})

        with patch.object(manager, "_describe_stack_target", return_value=target):
            functions = manager._collect_stack_functions("gco-monitoring")

        assert functions == {
            "region": "us-east-2",
            "stack_id": self._STACK_ID,
            "functions": [self._READER, self._AUTO_DELETE],
        }
        cloudformation.get_paginator.assert_called_once_with("list_stack_resources")
        cloudformation.get_paginator.return_value.paginate.assert_called_once_with(
            StackName=self._STACK_ID
        )

    def test_an_absent_stack_has_no_function_list(self):
        manager = self._manager()
        with patch.object(manager, "_describe_stack_target", return_value=None):
            assert manager._collect_stack_functions("gco-monitoring") is None

    def test_an_unreadable_stack_is_skipped_with_a_warning(self, caplog):
        manager = self._manager()
        with patch.object(
            manager, "_describe_stack_target", side_effect=RuntimeError("Throttling")
        ):
            assert manager._collect_stack_functions("gco-monitoring") is None
        assert "Could not list the Lambda functions of gco-monitoring: Throttling" in caplog.text

    # -- comparison ----------------------------------------------------

    def test_names_the_log_groups_of_the_functions_the_update_removed(self):
        manager = self._manager()
        cloudformation = self._cloudformation([self._resource(self._AUTO_DELETE)])

        with patch("boto3.client", return_value=cloudformation) as client:
            removed = manager._removed_function_log_groups("gco-monitoring", self._before())

        # The stack is read again by its ID, in its own Region.
        client.assert_called_once_with("cloudformation", region_name="us-east-2")
        cloudformation.get_paginator.return_value.paginate.assert_called_once_with(
            StackName=self._STACK_ID
        )
        assert removed == {
            "gco-monitoring": {
                "region": "us-east-2",
                "log_groups": [f"/aws/lambda/{self._READER}"],
            }
        }

    def test_an_update_that_kept_every_function_names_nothing(self):
        manager = self._manager()
        cloudformation = self._cloudformation(
            [self._resource(self._READER), self._resource(self._AUTO_DELETE)]
        )

        with patch("boto3.client", return_value=cloudformation):
            assert manager._removed_function_log_groups("gco-monitoring", self._before()) == {}

    def test_a_stack_that_cannot_be_read_again_names_nothing(self, caplog):
        manager = self._manager()
        cloudformation = self._cloudformation(error=RuntimeError("ExpiredToken"))

        with patch("boto3.client", return_value=cloudformation):
            assert manager._removed_function_log_groups("gco-monitoring", self._before()) == {}
        assert "Could not list the Lambda functions of gco-monitoring: ExpiredToken" in caplog.text

    def test_the_teardown_deletes_the_reader_group_by_its_exact_name(self, capsys):
        """The three steps chained the way ``destroy_orchestrated`` chains them."""
        manager = self._manager()
        before = self._cloudformation(
            [self._resource(self._READER), self._resource(self._AUTO_DELETE)]
        )
        after = self._cloudformation([self._resource(self._AUTO_DELETE)])
        logs = MagicMock()
        clients = {("cloudformation", "us-east-2"): after, ("logs", "us-east-2"): logs}

        with patch.object(
            manager,
            "_describe_stack_target",
            return_value=("us-east-2", before, {"StackId": self._STACK_ID}),
        ):
            functions = manager._collect_stack_functions("gco-monitoring")
        assert functions is not None
        with patch(
            "boto3.client", side_effect=lambda service, region_name: clients[(service, region_name)]
        ):
            removed = manager._removed_function_log_groups("gco-monitoring", functions)
            outcome = manager._cleanup_implicit_log_groups(removed, removed)

        logs.delete_log_group.assert_called_once_with(logGroupName=f"/aws/lambda/{self._READER}")
        assert outcome == {
            "deleted": [f"us-east-2:/aws/lambda/{self._READER}"],
            "missing": [],
            "errors": [],
        }
        assert "Deleted 1 implicit CloudWatch log group(s)" in capsys.readouterr().out


class TestBastionIamCleanup:
    """destroy-all retires the ephemeral-bastion IAM role + profile.

    ``destroy_ephemeral_bastion`` already does this on a clean tunnel
    close, but a killed process orphans the pair; both survived a real
    teardown and tripped the live-validation clean-account gate.
    """

    def _manager(self, project_name="gco"):
        from cli.stacks import StackManager

        config = MagicMock()
        config.project_name = project_name
        config.global_region = "us-east-2"
        return StackManager(config)

    def test_runs_the_exact_teardown_commands_in_order(self):
        manager = self._manager()
        with patch("cli.ephemeral_bastion._run_aws", return_value="") as run_aws:
            outcome = manager._cleanup_bastion_iam()

        operations = [call.args[0][2] for call in run_aws.call_args_list]
        assert operations == [
            "remove-role-from-instance-profile",
            "delete-instance-profile",
            "detach-role-policy",
            "delete-role",
        ]
        for call in run_aws.call_args_list:
            argv = call.args[0]
            assert argv[:2] == ["aws", "iam"]
            assert "gco-ephemeral-bastion" in " ".join(argv)
        assert outcome["completed_steps"] == 4
        assert outcome["errors"] == []

    def test_absent_role_and_profile_read_as_clean(self):
        """NoSuchEntity everywhere simply means nothing was orphaned."""
        manager = self._manager()
        with patch(
            "cli.ephemeral_bastion._run_aws",
            side_effect=RuntimeError("An error occurred (NoSuchEntity) ..."),
        ):
            outcome = manager._cleanup_bastion_iam()

        assert outcome["completed_steps"] == 0
        assert outcome["absent_steps"] == 4
        assert outcome["errors"] == []

    def test_step_failures_are_recorded_and_do_not_raise(self):
        manager = self._manager()
        with patch(
            "cli.ephemeral_bastion._run_aws",
            side_effect=RuntimeError("AccessDenied"),
        ):
            outcome = manager._cleanup_bastion_iam()

        assert outcome["completed_steps"] == 0
        assert len(outcome["errors"]) == 4

    def test_invalid_project_name_is_best_effort_not_fatal(self):
        manager = self._manager(project_name=MagicMock())
        with patch("cli.ephemeral_bastion._run_aws") as run_aws:
            outcome = manager._cleanup_bastion_iam()

        run_aws.assert_not_called()
        assert outcome["errors"]


class TestTrafficDialParameterCleanup:
    """destroy-all purges the runtime /{project}/traffic-dial SSM tree.

    The controller Lambda writes ``state`` and ``gco capacity traffic-dial
    set`` writes ``override-*`` at runtime, so CloudFormation never deletes
    them. A stale override surviving into the account's next deployment
    would silently pin that region's dial forever — the controller honors
    overrides indefinitely by design.
    """

    def _manager(self):
        from cli.stacks import StackManager

        config = MagicMock()
        config.project_name = "gco"
        config.global_region = "us-east-2"
        return StackManager(config)

    def test_reports_the_purged_names(self, capsys):
        manager = self._manager()
        deleted = ["/gco/traffic-dial/override-us-east-1", "/gco/traffic-dial/state"]
        with patch("cli.capacity.traffic_dial.TrafficDialManager") as dial_manager:
            dial_manager.return_value.purge_runtime_parameters.return_value = deleted
            outcome = manager._cleanup_traffic_dial_parameters()

        assert outcome == {"deleted": deleted, "errors": []}
        dial_manager.assert_called_once_with(manager.config)
        assert "2 runtime traffic-dial SSM" in capsys.readouterr().out

    def test_empty_tree_is_silent(self, capsys):
        manager = self._manager()
        with patch("cli.capacity.traffic_dial.TrafficDialManager") as dial_manager:
            dial_manager.return_value.purge_runtime_parameters.return_value = []
            outcome = manager._cleanup_traffic_dial_parameters()

        assert outcome == {"deleted": [], "errors": []}
        assert capsys.readouterr().out == ""

    def test_purge_failure_is_recorded_not_raised(self):
        manager = self._manager()
        with patch("cli.capacity.traffic_dial.TrafficDialManager") as dial_manager:
            dial_manager.return_value.purge_runtime_parameters.side_effect = RuntimeError(
                "AccessDenied"
            )
            outcome = manager._cleanup_traffic_dial_parameters()

        assert outcome["deleted"] == []
        assert outcome["errors"] == ["RuntimeError: AccessDenied"]


class TestAddonParameterCleanup:
    """destroy-all deletes a destroyed Region's /{project}/addons/{region} tree.

    The Helm orchestrator, installer, kubectl applier and teardown provider
    write the replay input, execution identity, status records and teardown
    fence there at runtime. CloudFormation never owned them, so a live
    two-Region teardown left 28 behind in each Region.
    """

    def _manager(self, project_name="gco"):
        from cli.stacks import StackManager

        config = MagicMock()
        config.project_name = project_name
        config.global_region = "us-east-2"
        return StackManager(config)

    @staticmethod
    def _ssm(names):
        ssm = MagicMock()
        paginator = ssm.get_paginator.return_value
        paginator.paginate.return_value = [
            {"Parameters": [{"Name": name} for name in names[:7]]},
            {"Parameters": [{"Name": name} for name in names[7:]] + [{"Type": "String"}]},
        ]
        ssm.delete_parameters.side_effect = lambda Names: {
            "DeletedParameters": list(Names),
            "InvalidParameters": [],
        }
        return ssm

    def test_deletes_the_whole_tree_in_batches_of_ten(self, capsys):
        manager = self._manager()
        names = [f"/gco/addons/us-west-2/chart-{index:02d}" for index in range(12)]
        ssm = self._ssm(names)
        with patch("boto3.client", return_value=ssm) as client:
            outcome = manager._cleanup_addon_parameters("gco-us-west-2")

        # The Region comes from the stack name when the caller has no record.
        client.assert_called_once_with("ssm", region_name="us-west-2")
        ssm.get_paginator.assert_called_once_with("get_parameters_by_path")
        ssm.get_paginator.return_value.paginate.assert_called_once_with(
            Path="/gco/addons/us-west-2", Recursive=True
        )
        assert [len(call.kwargs["Names"]) for call in ssm.delete_parameters.call_args_list] == [
            10,
            2,
        ]
        assert outcome == {
            "stack": "gco-us-west-2",
            "region": "us-west-2",
            "deleted": sorted(names),
            "errors": [],
        }
        assert "Deleted 12 add-on SSM parameter(s) under /gco/addons/us-west-2." in (
            capsys.readouterr().out
        )

    def test_uses_the_recorded_region_and_project_scope(self):
        manager = self._manager(project_name="acme")
        ssm = self._ssm([])
        with patch("boto3.client", return_value=ssm) as client:
            outcome = manager._cleanup_addon_parameters("acme-us-east-1", region="eu-west-1")

        client.assert_called_once_with("ssm", region_name="eu-west-1")
        ssm.get_paginator.return_value.paginate.assert_called_once_with(
            Path="/acme/addons/eu-west-1", Recursive=True
        )
        ssm.delete_parameters.assert_not_called()
        assert outcome["deleted"] == [] and outcome["errors"] == []

    def test_listing_failure_is_recorded_not_raised(self, capsys):
        manager = self._manager()
        ssm = MagicMock()
        ssm.get_paginator.return_value.paginate.side_effect = RuntimeError("AccessDenied")
        with patch("boto3.client", return_value=ssm):
            outcome = manager._cleanup_addon_parameters("gco-us-east-1")

        ssm.delete_parameters.assert_not_called()
        assert outcome["errors"] == ["/gco/addons/us-east-1: RuntimeError: AccessDenied"]
        assert capsys.readouterr().out == ""

    def test_one_failed_batch_does_not_stop_the_others(self):
        manager = self._manager()
        names = [f"/gco/addons/us-east-1/chart-{index:02d}" for index in range(12)]
        ssm = self._ssm(names)
        deleted_second = {"DeletedParameters": names[10:], "InvalidParameters": []}
        ssm.delete_parameters.side_effect = [RuntimeError("Throttling"), deleted_second]
        with patch("boto3.client", return_value=ssm):
            outcome = manager._cleanup_addon_parameters("gco-us-east-1")

        assert outcome["deleted"] == names[10:]
        assert outcome["errors"] == [", ".join(names[:10]) + ": RuntimeError: Throttling"]

    def test_a_stack_other_than_a_base_regional_one_is_skipped_whatever_its_region(self):
        """The analytics stack lives in the API Gateway Region, which can host a regional stack.

        Resolved to that Region, the sweep would delete the live regional
        stack's records, so only ``<project>-<region>`` stacks are swept.
        """
        manager = self._manager()
        with patch("boto3.client") as client:
            for stack_name in ("gco-analytics", "gco-regional-api-us-east-1", "gco-global"):
                outcome = manager._cleanup_addon_parameters(stack_name, region="us-east-1")
                assert outcome["skipped"] == "not-a-regional-stack"
                assert outcome["deleted"] == [] and outcome["errors"] == []
            assert manager._cleanup_addon_parameters("gco-analytics")["region"] is None
        client.assert_not_called()


class TestDestroyOrchestratedSweepsLogGroupsOnException:
    """An exception that ends the teardown still sweeps the stacks already deleted.

    Their log-group names and stack IDs were collected from live stacks, so a
    re-run cannot find them: skipping the sweep leaked them for good.
    """

    _STACKS = ["gco-global", "gco-us-east-1", "gco-us-west-2", "gco-monitoring"]
    _COLLECTED = {
        name: {"region": "us-east-1", "log_groups": [f"/aws/lambda/{name}"]} for name in _STACKS
    }
    _IDENTITIES = {name: {"region": "us-east-1", "stack_id": f"arn:{name}"} for name in _STACKS}

    def _run(
        self,
        *,
        destroy,
        present,
        parallel=False,
        on_cleanup=None,
        security_groups=None,
        parallel_destroy=None,
    ):
        from contextlib import ExitStack

        from cli.stacks import StackManager

        config = MagicMock()
        config.project_name = "gco"
        config.global_region = "us-east-2"
        cleanups: list[str] = []

        def record(name, _details):
            cleanups.append(name)
            if on_cleanup is not None:
                on_cleanup(name)

        def exists(stack_name, *_args, **_kwargs):
            if isinstance(present.get(stack_name), Exception):
                raise present[stack_name]
            return present.get(stack_name, True)

        with ExitStack() as stack:
            stack.enter_context(
                patch.object(StackManager, "list_stacks", return_value=list(self._STACKS))
            )
            stack.enter_context(
                patch.object(StackManager, "_start_eks_sg_watchdog", return_value=MagicMock())
            )
            stack.enter_context(
                patch.object(
                    StackManager,
                    "_cleanup_eks_security_groups",
                    side_effect=security_groups,
                    return_value={"errors": [], "blocked_by_enis": []},
                )
            )
            stack.enter_context(
                patch.object(StackManager, "_destroy_phase_remaining_stacks", return_value=[])
            )
            stack.enter_context(
                patch.object(StackManager, "_stack_exists_in_cloudformation", side_effect=exists)
            )
            stack.enter_context(patch.object(StackManager, "destroy", side_effect=destroy))
            if parallel_destroy is not None:
                stack.enter_context(
                    patch.object(
                        StackManager, "_destroy_stacks_parallel", side_effect=parallel_destroy
                    )
                )
            stack.enter_context(
                patch.object(
                    StackManager, "_collect_implicit_log_groups", return_value=self._COLLECTED
                )
            )
            stack.enter_context(
                patch.object(
                    StackManager, "_collect_stack_identities", return_value=self._IDENTITIES
                )
            )
            implicit = stack.enter_context(
                patch.object(
                    StackManager,
                    "_cleanup_implicit_log_groups",
                    return_value={"deleted": [], "missing": [], "errors": []},
                )
            )
            regenerated = stack.enter_context(
                patch.object(
                    StackManager,
                    "_cleanup_regenerated_log_groups",
                    return_value={"deleted": [], "errors": []},
                )
            )
            try:
                result = StackManager(config).destroy_orchestrated(
                    force=True, parallel=parallel, on_cleanup_complete=record
                )
            except BaseException as exc:
                result = exc
        return result, implicit, regenerated, cleanups

    def test_an_exception_from_destroy_sweeps_what_was_already_deleted(self):
        def destroy(stack_name, **_kwargs):
            if stack_name == "gco-monitoring":
                return True
            raise RuntimeError("ExpiredToken")

        result, implicit, regenerated, cleanups = self._run(
            destroy=destroy, present={"gco-monitoring": False}
        )

        assert isinstance(result, RuntimeError) and str(result) == "ExpiredToken"
        implicit.assert_called_once_with(self._COLLECTED, ["gco-monitoring"])
        regenerated.assert_called_once_with(self._IDENTITIES, ["gco-monitoring"])
        assert {"implicit-log-groups", "regenerated-log-groups"} <= set(cleanups)
        assert "traffic-dial-parameters" not in cleanups

    def test_a_parallel_phase_cut_short_is_swept_by_confirmed_absence(self):
        """The interrupted batch never reports what it deleted; CloudFormation does."""

        def parallel_destroy(**_kwargs):
            raise KeyboardInterrupt

        result, implicit, _regenerated, _cleanups = self._run(
            destroy=lambda stack_name, **_kwargs: True,
            present={"gco-monitoring": False, "gco-us-east-1": False},
            parallel=True,
            parallel_destroy=parallel_destroy,
        )

        assert isinstance(result, KeyboardInterrupt)
        implicit.assert_called_once_with(self._COLLECTED, ["gco-monitoring", "gco-us-east-1"])

    def test_an_exception_from_the_watchdog_cleanup_is_swept(self):
        result, implicit, _regenerated, _cleanups = self._run(
            destroy=lambda stack_name, **_kwargs: True,
            present={"gco-monitoring": False, "gco-us-east-1": False, "gco-us-west-2": False},
            security_groups=RuntimeError("Throttling"),
        )

        assert isinstance(result, RuntimeError) and str(result) == "Throttling"
        implicit.assert_called_once_with(
            self._COLLECTED, ["gco-monitoring", "gco-us-east-1", "gco-us-west-2"]
        )

    def test_an_unconfirmed_absence_keeps_that_stacks_log_groups(self, caplog):
        def destroy(stack_name, **_kwargs):
            raise RuntimeError("ExpiredToken")

        result, implicit, _regenerated, _cleanups = self._run(
            destroy=destroy,
            present={"gco-monitoring": OSError("unreachable"), "gco-us-east-1": False},
        )

        assert isinstance(result, RuntimeError)
        implicit.assert_called_once_with(self._COLLECTED, ["gco-us-east-1"])
        assert "Could not confirm gco-monitoring is deleted" in caplog.text

    def test_a_failing_sweep_never_masks_the_error_that_ended_the_teardown(self, caplog):
        def destroy(stack_name, **_kwargs):
            raise RuntimeError("ExpiredToken")

        def on_cleanup(name):
            if name == "implicit-log-groups":
                raise OSError("checkpoint write failed")

        result, _implicit, _regenerated, _cleanups = self._run(
            destroy=destroy, present={}, on_cleanup=on_cleanup
        )

        assert isinstance(result, RuntimeError) and str(result) == "ExpiredToken"
        assert "Log-group sweep after a failed teardown failed" in caplog.text

    def test_an_exception_after_finish_started_is_not_swept_twice(self):
        def on_cleanup(name):
            if name == "traffic-dial-parameters":
                raise OSError("checkpoint write failed")

        result, implicit, regenerated, _cleanups = self._run(
            destroy=lambda stack_name, **_kwargs: True, present={}, on_cleanup=on_cleanup
        )

        assert isinstance(result, OSError)
        implicit.assert_called_once()
        regenerated.assert_called_once()


class TestDestroyOrchestratedImplicitCleanupWiring:
    """The sweep runs on every non-strict exit path and never in strict mode."""

    def _run(self, *, destroy_results, strict=False, collected=None):
        from cli.stacks import StackManager

        config = MagicMock()
        config.project_name = "gco"
        config.global_region = "us-east-2"
        stacks = ["gco-global", "gco-us-east-1"]
        cleanups: list[tuple[str, dict]] = []

        base_patches = [
            patch.object(StackManager, "list_stacks", return_value=stacks),
            patch.object(StackManager, "cleanup_orphaned_bastions", return_value=0),
            patch.object(
                StackManager,
                "_cleanup_backup_vault",
                return_value={"errors": []},
            ),
            patch.object(StackManager, "_start_eks_sg_watchdog", return_value=MagicMock()),
            patch.object(
                StackManager,
                "_cleanup_eks_security_groups",
                return_value={"errors": [], "blocked_by_enis": []},
            ),
            patch.object(StackManager, "_destroy_phase_remaining_stacks", return_value=[]),
            patch.object(StackManager, "destroy", side_effect=destroy_results),
            patch.object(
                StackManager,
                "_cleanup_cluster_volumes",
                return_value={"deleted": [], "surviving": [], "errors": []},
            ),
        ]
        if strict:
            base_patches.append(
                patch.object(StackManager, "_resolve_strict_teardown_resources", return_value={})
            )

        kwargs = {}
        if strict:
            kwargs = {
                "expected_stack_ids": {
                    "gco-global": "arn:aws:cloudformation:us-east-2:1:stack/gco-global/x",
                    "gco-us-east-1": "arn:aws:cloudformation:us-east-1:1:stack/gco-us-east-1/y",
                },
                "authorize_stack": lambda *_args: None,
            }

        from contextlib import ExitStack

        with ExitStack() as stack:
            for item in base_patches:
                stack.enter_context(item)
            collect = stack.enter_context(
                patch.object(
                    StackManager,
                    "_collect_implicit_log_groups",
                    return_value=collected if collected is not None else {},
                )
            )
            cleanup = stack.enter_context(
                patch.object(
                    StackManager,
                    "_cleanup_implicit_log_groups",
                    return_value={"deleted": [], "missing": [], "errors": []},
                )
            )
            bastion_iam = stack.enter_context(
                patch.object(
                    StackManager,
                    "_cleanup_bastion_iam",
                    return_value={"completed_steps": 0, "absent_steps": 4, "errors": []},
                )
            )
            dial = stack.enter_context(
                patch.object(
                    StackManager,
                    "_cleanup_traffic_dial_parameters",
                    return_value={"deleted": [], "errors": []},
                )
            )
            manager = StackManager(config)
            result = manager.destroy_orchestrated(
                force=True,
                on_cleanup_complete=lambda name, details: cleanups.append((name, details)),
                **kwargs,
            )
        return result, collect, cleanup, bastion_iam, dial, cleanups

    def test_full_success_sweeps_collected_groups(self):
        collected = {"gco-us-east-1": {"region": "us-east-1", "log_groups": ["/aws/lambda/x"]}}
        (ok, _successful, failed), collect, cleanup, bastion_iam, dial, cleanups = self._run(
            destroy_results=[True, True],
            collected=collected,
        )

        assert ok is True and failed == []
        collect.assert_called_once()
        cleanup.assert_called_once()
        assert cleanup.call_args.args[0] == collected
        assert sorted(cleanup.call_args.args[1]) == ["gco-global", "gco-us-east-1"]
        bastion_iam.assert_called_once()
        dial.assert_called_once()
        assert {name for name, _ in cleanups} >= {
            "bastions",
            "bastion-iam",
            "implicit-log-groups",
            "traffic-dial-parameters",
            "dynamic-pvs",
        }

    def test_partial_failure_still_sweeps_the_destroyed_stacks(self):
        """gco-us-east-1 deletes, gco-global fails: its groups still go."""
        collected = {
            "gco-us-east-1": {"region": "us-east-1", "log_groups": ["/aws/lambda/x"]},
            "gco-global": {"region": "us-east-2", "log_groups": ["/aws/lambda/y"]},
        }
        (ok, successful, failed), _collect, cleanup, _bastion, dial, _cleanups = self._run(
            destroy_results=[True, False],
            collected=collected,
        )

        assert ok is False
        assert successful == ["gco-us-east-1"]
        assert failed == ["gco-global"]
        cleanup.assert_called_once()
        assert cleanup.call_args.args[1] == ["gco-us-east-1"]
        # A surviving stack can mean a live accelerator whose manual override
        # is standing operator intent: the purge must not run.
        dial.assert_not_called()

    def test_nothing_collected_records_no_sweep(self):
        (ok, _successful, _failed), collect, cleanup, bastion_iam, _dial, cleanups = self._run(
            destroy_results=[True, True],
            collected={},
        )

        assert ok is True
        collect.assert_called_once()
        cleanup.assert_not_called()
        bastion_iam.assert_called_once()
        assert "implicit-log-groups" not in {name for name, _ in cleanups}

    def test_regional_volume_sweep_waits_for_confirmed_stack_absence(self):
        """A regional stack that did not delete leaves its volumes untouched.

        While the stack survives so may its EKS cluster, and a detached volume
        belonging to a live cluster can simply be between pod restarts.
        """
        (ok, _successful, failed), _collect, _cleanup, _bastion, _dial, cleanups = self._run(
            destroy_results=[True, False],
        )

        assert ok is False and failed == ["gco-global"]
        # gco-global fails, so the pre-regional phase never completes; the
        # regional barrier was still reached, so the sweep did run for the
        # regional stack that is confirmed gone.
        assert "dynamic-pvs" in {name for name, _ in cleanups}

    def test_volume_cleanup_failure_never_relabels_a_deleted_stack(self):
        """Storage cleanup and stack deletion are tracked separately.

        Marking a deleted stack failed would send the CLI's retry loop back to
        CDK for a stack that no longer exists.
        """
        from cli.stacks import StackManager

        config = MagicMock()
        config.project_name = "gco"
        config.global_region = "us-east-2"
        cleanups: list[tuple[str, dict]] = []

        with (
            patch.object(StackManager, "list_stacks", return_value=["gco-global", "gco-us-east-1"]),
            patch.object(StackManager, "cleanup_orphaned_bastions", return_value=0),
            patch.object(StackManager, "_cleanup_backup_vault", return_value={"errors": []}),
            patch.object(StackManager, "_start_eks_sg_watchdog", return_value=MagicMock()),
            patch.object(
                StackManager,
                "_cleanup_eks_security_groups",
                return_value={"errors": [], "blocked_by_enis": []},
            ),
            patch.object(StackManager, "_destroy_phase_remaining_stacks", return_value=[]),
            patch.object(StackManager, "destroy", return_value=True),
            patch.object(StackManager, "_collect_implicit_log_groups", return_value={}),
            patch.object(
                StackManager,
                "_cleanup_bastion_iam",
                return_value={"completed_steps": 0, "absent_steps": 0, "errors": []},
            ),
            patch.object(
                StackManager,
                "_cleanup_traffic_dial_parameters",
                return_value={"deleted": [], "errors": []},
            ),
            patch.object(
                StackManager,
                "_cleanup_cluster_volumes",
                return_value={"deleted": [], "surviving": [], "errors": [{"error": "denied"}]},
            ),
        ):
            ok, _successful, failed = StackManager(config).destroy_orchestrated(
                force=True,
                on_cleanup_complete=lambda name, details: cleanups.append((name, details)),
            )

        assert ok is True
        assert failed == []
        assert (
            "dynamic-pvs",
            {"deleted": [], "surviving": [], "errors": [{"error": "denied"}]},
        ) in (cleanups)

    def test_retain_volumes_reaches_the_worker(self):
        from cli.stacks import StackManager

        config = MagicMock()
        config.project_name = "gco"
        config.global_region = "us-east-2"

        with (
            patch.object(StackManager, "list_stacks", return_value=["gco-global", "gco-us-east-1"]),
            patch.object(StackManager, "cleanup_orphaned_bastions", return_value=0),
            patch.object(StackManager, "_cleanup_backup_vault", return_value={"errors": []}),
            patch.object(StackManager, "_start_eks_sg_watchdog", return_value=MagicMock()),
            patch.object(
                StackManager,
                "_cleanup_eks_security_groups",
                return_value={"errors": [], "blocked_by_enis": []},
            ),
            patch.object(StackManager, "_destroy_phase_remaining_stacks", return_value=[]),
            patch.object(StackManager, "destroy", return_value=True),
            patch.object(StackManager, "_collect_implicit_log_groups", return_value={}),
            patch.object(
                StackManager,
                "_cleanup_bastion_iam",
                return_value={"completed_steps": 0, "absent_steps": 0, "errors": []},
            ),
            patch.object(
                StackManager,
                "_cleanup_traffic_dial_parameters",
                return_value={"deleted": [], "errors": []},
            ),
            patch.object(
                StackManager,
                "_cleanup_cluster_volumes",
                return_value={"deleted": [], "surviving": [], "errors": []},
            ) as worker,
        ):
            StackManager(config).destroy_orchestrated(force=True, retain_volumes=True)

        worker.assert_called_once_with("gco-us-east-1", region=None, retain=True)

    def test_strict_teardown_skips_the_log_sweep_but_retires_bastion_iam_and_dial_tree(self):
        """The live-validation harness owns fenced log-group deletion, so strict
        mode skips that sweep. Two things nothing else removes still go: the
        runtime traffic-dial parameters (untagged, so the harness's
        tagging-index audit cannot see them) once the whole teardown
        succeeds, and the bastions' shared IAM role and instance profile,
        which closing a tunnel leaves in place. A live run's final inventory
        found that pair after a strict teardown that skipped it."""
        (ok, _successful, failed), collect, cleanup, bastion_iam, dial, cleanups = self._run(
            destroy_results=[True, True],
            strict=True,
        )

        assert ok is True and failed == []
        collect.assert_not_called()
        cleanup.assert_not_called()
        bastion_iam.assert_called_once_with()
        dial.assert_called_once()
        assert {"bastion-iam", "traffic-dial-parameters"} <= {name for name, _ in cleanups}

    def test_regenerated_log_groups_are_swept_only_for_destroyed_stacks(self):
        """gco-us-east-1 deletes and gco-global fails: only the former is swept."""
        from cli.stacks import StackManager

        identities = {
            "gco-us-east-1": {"region": "us-east-1", "stack_id": "stack-id-1"},
            "gco-global": {"region": "us-east-2", "stack_id": "stack-id-2"},
        }
        with (
            patch.object(
                StackManager, "_collect_stack_identities", return_value=identities
            ) as collect,
            patch.object(
                StackManager,
                "_cleanup_regenerated_log_groups",
                return_value={"deleted": [], "errors": []},
            ) as sweep,
        ):
            (ok, _successful, failed), *_sweeps, cleanups = self._run(destroy_results=[True, False])

        assert ok is False and failed == ["gco-global"]
        collect.assert_called_once_with(["gco-global", "gco-us-east-1"])
        sweep.assert_called_once_with(identities, ["gco-us-east-1"])
        assert ("regenerated-log-groups", {"deleted": [], "errors": []}) in cleanups

    @pytest.mark.parametrize("strict", [False, True])
    def test_addon_parameters_go_once_the_regional_stack_is_gone(self, strict):
        """Strict teardowns purge them too: nothing else owns these untagged params."""
        from cli.stacks import StackManager

        outcome = {"stack": "gco-us-east-1", "region": "us-east-1", "deleted": [], "errors": []}
        with patch.object(StackManager, "_cleanup_addon_parameters", return_value=outcome) as sweep:
            (ok, _successful, _failed), *_sweeps, cleanups = self._run(
                destroy_results=[True, True],
                strict=strict,
            )

        assert ok is True
        sweep.assert_called_once_with("gco-us-east-1", region=None)
        assert ("addon-parameters", outcome) in cleanups

    def test_addon_parameters_stay_while_a_regional_stack_survives(self):
        from cli.stacks import StackManager

        with patch.object(StackManager, "_cleanup_addon_parameters") as sweep:
            (ok, _successful, failed), *_sweeps, cleanups = self._run(destroy_results=[False])

        assert ok is False and failed == ["gco-us-east-1"]
        sweep.assert_not_called()
        assert "addon-parameters" not in {name for name, _ in cleanups}

    def test_strict_teardown_never_sweeps_regenerated_log_groups(self):
        from cli.stacks import StackManager

        with (
            patch.object(StackManager, "_collect_stack_identities") as collect,
            patch.object(StackManager, "_cleanup_regenerated_log_groups") as sweep,
        ):
            (ok, _successful, _failed), *_sweeps, cleanups = self._run(
                destroy_results=[True, True],
                strict=True,
            )

        assert ok is True
        collect.assert_not_called()
        sweep.assert_not_called()
        assert "regenerated-log-groups" not in {name for name, _ in cleanups}


class TestKeepControlPlaneTeardown:
    """``destroy_orchestrated(keep_control_plane=True)`` removes only the workload tier.

    The global, API Gateway and monitoring stacks stay, the monitoring stack is
    first updated in place so it stops referencing the regional stacks, and the
    sweeps that only make sense for a full teardown (backup-vault purge,
    bastion IAM retirement, traffic-dial purge) never run.
    """

    _ALL_STACKS = [
        "gco-global",
        "gco-api-gateway",
        "gco-us-east-1",
        "gco-regional-api-us-east-1",
        "gco-monitoring",
    ]

    # The monitoring stack's functions before the detach, and the log group of
    # the export reader the detach removed.
    _READER = "gco-monitoring-CustomCrossRegionExportReaderCustom-abc123"
    _FUNCTIONS = {
        "region": "us-east-2",
        "stack_id": "arn:aws:cloudformation:us-east-2:111111111111:stack/gco-monitoring/x",
        "functions": [_READER, "gco-monitoring-CustomS3AutoDeleteObjectsCustomReso-def456"],
    }
    _REMOVED = {"gco-monitoring": {"region": "us-east-2", "log_groups": [f"/aws/lambda/{_READER}"]}}
    _SWEPT = {"deleted": [f"us-east-2:/aws/lambda/{_READER}"], "missing": [], "errors": []}

    def _run(
        self,
        *,
        detach_result: bool = True,
        destroy_result: bool | Exception = True,
        functions: dict | None = None,
        removed: dict | None = None,
        strict: bool = False,
    ):
        from contextlib import ExitStack

        from cli.stacks import StackManager

        config = MagicMock()
        config.project_name = "gco"
        config.global_region = "us-east-2"
        cleanups: list[tuple[str, dict]] = []
        destroyed: list[str] = []
        events: list[str] = []

        def fake_destroy(stack_name, **_kwargs):
            destroyed.append(stack_name)
            events.append(f"destroy {stack_name}")
            if isinstance(destroy_result, Exception):
                raise destroy_result
            return destroy_result

        def fake_detach():
            events.append("detach")
            return detach_result

        def fake_list_functions(stack_name):
            events.append(f"list functions of {stack_name}")
            return functions

        def fake_compare(stack_name, _before):
            events.append(f"compare functions of {stack_name}")
            return removed or {}

        def fake_sweep(_collected, _stacks):
            events.append("delete removed-function log groups")
            return dict(self._SWEPT)

        strict_kwargs = {}
        if strict:
            strict_kwargs = {
                "expected_stack_ids": {name: f"arn:{name}" for name in self._ALL_STACKS},
                "authorize_stack": MagicMock(),
            }
        with ExitStack() as stack:
            stack.enter_context(
                patch.object(StackManager, "list_stacks", return_value=list(self._ALL_STACKS))
            )
            detach = stack.enter_context(
                patch.object(
                    StackManager, "detach_monitoring_from_regions", side_effect=fake_detach
                )
            )
            bastions = stack.enter_context(
                patch.object(StackManager, "cleanup_orphaned_bastions", return_value=0)
            )
            vault = stack.enter_context(
                patch.object(StackManager, "_cleanup_backup_vault", return_value={"errors": []})
            )
            bastion_iam = stack.enter_context(
                patch.object(StackManager, "_cleanup_bastion_iam", return_value={"errors": []})
            )
            stack.enter_context(
                patch.object(StackManager, "_resolve_strict_teardown_resources", return_value={})
            )
            stack.enter_context(
                patch.object(StackManager, "_start_eks_sg_watchdog", return_value=MagicMock())
            )
            stack.enter_context(
                patch.object(
                    StackManager,
                    "_cleanup_eks_security_groups",
                    return_value={"errors": [], "blocked_by_enis": []},
                )
            )
            stack.enter_context(
                patch.object(StackManager, "_destroy_phase_remaining_stacks", return_value=[])
            )
            stack.enter_context(patch.object(StackManager, "destroy", side_effect=fake_destroy))
            collect = stack.enter_context(
                patch.object(StackManager, "_collect_implicit_log_groups", return_value={})
            )
            list_functions = stack.enter_context(
                patch.object(
                    StackManager, "_collect_stack_functions", side_effect=fake_list_functions
                )
            )
            compare = stack.enter_context(
                patch.object(StackManager, "_removed_function_log_groups", side_effect=fake_compare)
            )
            sweep = stack.enter_context(
                patch.object(StackManager, "_cleanup_implicit_log_groups", side_effect=fake_sweep)
            )
            dial = stack.enter_context(
                patch.object(
                    StackManager,
                    "_cleanup_traffic_dial_parameters",
                    return_value={"deleted": [], "errors": []},
                )
            )
            volumes = stack.enter_context(
                patch.object(
                    StackManager,
                    "_cleanup_cluster_volumes",
                    return_value={"deleted": [], "surviving": [], "errors": []},
                )
            )
            try:
                result = StackManager(config).destroy_orchestrated(
                    force=True,
                    keep_control_plane=True,
                    on_cleanup_complete=lambda name, details: cleanups.append((name, details)),
                    **strict_kwargs,
                )
            except Exception as exc:
                result = exc
        mocks = {
            "detach": detach,
            "bastions": bastions,
            "vault": vault,
            "bastion_iam": bastion_iam,
            "collect": collect,
            "list_functions": list_functions,
            "compare": compare,
            "sweep": sweep,
            "dial": dial,
            "volumes": volumes,
            "events": events,
        }
        return result, destroyed, cleanups, mocks

    def test_only_the_workload_tier_is_destroyed_and_global_sweeps_are_skipped(self):
        (ok, successful, failed), destroyed, cleanups, mocks = self._run()

        assert ok is True and failed == []
        # Bridges before base regional stacks; nothing else is touched.
        assert destroyed == ["gco-regional-api-us-east-1", "gco-us-east-1"]
        assert sorted(successful) == ["gco-regional-api-us-east-1", "gco-us-east-1"]
        mocks["detach"].assert_called_once_with()
        mocks["vault"].assert_not_called()
        mocks["bastion_iam"].assert_not_called()
        mocks["dial"].assert_not_called()
        # Per-stack sweeps still run for the stacks that went away.
        mocks["volumes"].assert_called_once_with("gco-us-east-1", region=None, retain=False)
        assert mocks["bastions"].call_args.args[0] == [
            "gco-us-east-1",
            "gco-regional-api-us-east-1",
        ]
        assert mocks["collect"].call_args.args[0] == [
            "gco-us-east-1",
            "gco-regional-api-us-east-1",
        ]
        recorded = {name for name, _ in cleanups}
        assert "dynamic-pvs" in recorded
        assert {"backup-vault", "bastion-iam", "traffic-dial-parameters"}.isdisjoint(recorded)

    def test_a_failed_monitoring_detach_stops_before_any_deletion(self):
        (ok, successful, failed), destroyed, _cleanups, mocks = self._run(detach_result=False)

        assert ok is False
        assert successful == []
        assert failed == ["gco-monitoring"]
        assert destroyed == []
        mocks["bastions"].assert_not_called()

    def test_a_failed_regional_delete_is_reported_without_global_sweeps(self):
        (ok, _successful, failed), destroyed, _cleanups, mocks = self._run(destroy_result=False)

        assert ok is False
        assert "gco-regional-api-us-east-1" in failed
        assert destroyed == ["gco-regional-api-us-east-1"]
        mocks["dial"].assert_not_called()

    def test_the_log_groups_of_functions_the_detach_removed_go_after_the_regional_stacks(self):
        """The detach deletes the export reader's function, never its log group."""
        (ok, _successful, failed), _destroyed, cleanups, mocks = self._run(
            functions=self._FUNCTIONS, removed=self._REMOVED
        )

        assert ok is True and failed == []
        assert mocks["events"] == [
            "list functions of gco-monitoring",
            "detach",
            "compare functions of gco-monitoring",
            "destroy gco-regional-api-us-east-1",
            "destroy gco-us-east-1",
            "delete removed-function log groups",
        ]
        mocks["compare"].assert_called_once_with("gco-monitoring", self._FUNCTIONS)
        mocks["sweep"].assert_called_once_with(self._REMOVED, self._REMOVED)
        assert ("removed-function-log-groups", self._SWEPT) in cleanups

    def test_a_failed_regional_delete_still_deletes_them(self):
        (ok, _successful, failed), _destroyed, cleanups, mocks = self._run(
            destroy_result=False, functions=self._FUNCTIONS, removed=self._REMOVED
        )

        assert ok is False and failed == ["gco-regional-api-us-east-1"]
        mocks["sweep"].assert_called_once_with(self._REMOVED, self._REMOVED)
        assert ("removed-function-log-groups", self._SWEPT) in cleanups

    def test_an_error_that_ends_the_teardown_still_deletes_them(self):
        result, _destroyed, cleanups, mocks = self._run(
            destroy_result=RuntimeError("ExpiredToken"),
            functions=self._FUNCTIONS,
            removed=self._REMOVED,
        )

        assert isinstance(result, RuntimeError) and str(result) == "ExpiredToken"
        mocks["sweep"].assert_called_once_with(self._REMOVED, self._REMOVED)
        assert ("removed-function-log-groups", self._SWEPT) in cleanups

    def test_a_failed_detach_deletes_no_log_group(self):
        (ok, _successful, _failed), _destroyed, cleanups, mocks = self._run(
            detach_result=False, functions=self._FUNCTIONS, removed=self._REMOVED
        )

        assert ok is False
        mocks["compare"].assert_not_called()
        mocks["sweep"].assert_not_called()
        assert cleanups == []

    def test_an_update_that_removed_no_function_records_no_sweep(self):
        (ok, _successful, _failed), _destroyed, cleanups, mocks = self._run(
            functions=self._FUNCTIONS, removed={}
        )

        assert ok is True
        mocks["compare"].assert_called_once_with("gco-monitoring", self._FUNCTIONS)
        mocks["sweep"].assert_not_called()
        assert "removed-function-log-groups" not in {name for name, _ in cleanups}

    def test_an_absent_or_unreadable_monitoring_stack_is_not_compared(self):
        (ok, _successful, _failed), _destroyed, _cleanups, mocks = self._run(functions=None)

        assert ok is True
        mocks["list_functions"].assert_called_once_with("gco-monitoring")
        mocks["compare"].assert_not_called()
        mocks["sweep"].assert_not_called()

    def test_strict_teardowns_list_no_monitoring_functions(self):
        """The live-validation harness owns log-group deletion in strict teardowns."""
        (ok, _successful, _failed), _destroyed, cleanups, mocks = self._run(
            strict=True, functions=self._FUNCTIONS, removed=self._REMOVED
        )

        assert ok is True
        mocks["detach"].assert_called_once_with()
        mocks["list_functions"].assert_not_called()
        mocks["compare"].assert_not_called()
        mocks["sweep"].assert_not_called()
        assert "removed-function-log-groups" not in {name for name, _ in cleanups}


class TestGlobalStackBackupVaultRace:
    """The global vault is emptied again right before its stack is deleted.

    The first sweep runs when the teardown starts; the global stack goes last,
    often an hour later, and a scheduled backup in between left the vault
    non-empty (a live run hit DELETE_FAILED at 04:48:56 after starting at 03:35).
    """

    _ALL_STACKS = ["gco-global", "gco-api-gateway", "gco-us-east-1"]

    def _run(self, *, destroy_results, sweeps, strict=False):
        from contextlib import ExitStack

        from cli.stacks import StackManager

        config = MagicMock()
        config.project_name = "gco"
        config.global_region = "us-east-2"
        events: list[str] = []
        cleanups: list[tuple[str, dict]] = []
        destroy_results = iter(destroy_results)
        sweeps = iter(sweeps)

        def fake_destroy(stack_name, **_kwargs):
            events.append(f"destroy {stack_name}")
            return next(destroy_results)

        def fake_sweep(**kwargs):
            events.append("sweep")
            return next(sweeps)

        strict_kwargs = {}
        if strict:
            strict_kwargs = {
                "expected_stack_ids": {name: f"arn:{name}" for name in self._ALL_STACKS},
                "authorize_stack": MagicMock(),
            }
        with ExitStack() as stack:
            stack.enter_context(
                patch.object(StackManager, "list_stacks", return_value=list(self._ALL_STACKS))
            )
            stack.enter_context(
                patch.object(StackManager, "_resolve_strict_teardown_resources", return_value={})
            )
            vault = stack.enter_context(
                patch.object(StackManager, "_cleanup_backup_vault", side_effect=fake_sweep)
            )
            stack.enter_context(
                patch.object(StackManager, "_destroy_phase_remaining_stacks", return_value=[])
            )
            stack.enter_context(patch.object(StackManager, "destroy", side_effect=fake_destroy))
            result = StackManager(config).destroy_orchestrated(
                force=True,
                on_cleanup_complete=lambda name, details: cleanups.append((name, details)),
                **strict_kwargs,
            )
        return result, events, cleanups, vault

    def test_the_vault_is_swept_again_immediately_before_the_global_delete(self):
        empty = {"status": "completed", "resource_status": "CREATE_COMPLETE", "errors": []}
        (ok, successful, failed), events, cleanups, vault = self._run(
            destroy_results=[True, True, True], sweeps=[empty, empty]
        )

        assert ok is True and failed == []
        assert successful == ["gco-us-east-1", "gco-api-gateway", "gco-global"]
        assert events == [
            "sweep",
            "destroy gco-us-east-1",
            "destroy gco-api-gateway",
            "sweep",
            "destroy gco-global",
        ]
        assert [name for name, _ in cleanups if name == "backup-vault"] == ["backup-vault"] * 2
        for call_ in vault.call_args_list:
            assert call_.kwargs == {
                "expected_stack_id": None,
                "authorize_stack": None,
                "require_expected_identity": False,
            }

    def test_a_delete_the_vault_blocked_is_retried_once_after_another_sweep(self, capsys):
        empty = {"status": "completed", "resource_status": "CREATE_COMPLETE", "errors": []}
        blocked = {"status": "completed", "resource_status": "DELETE_FAILED", "errors": []}
        (ok, successful, failed), events, _cleanups, _vault = self._run(
            destroy_results=[True, True, False, True], sweeps=[empty, empty, blocked]
        )

        assert ok is True and failed == []
        assert successful[-1] == "gco-global"
        assert events[-4:] == ["sweep", "destroy gco-global", "sweep", "destroy gco-global"]
        assert "its backup vault was not empty; retrying the delete once" in (
            capsys.readouterr().out
        )

    def test_a_delete_that_failed_elsewhere_is_not_retried(self):
        empty = {"status": "completed", "resource_status": "CREATE_COMPLETE", "errors": []}
        absent = {"status": "vault-resource-absent", "errors": []}
        (ok, _successful, failed), events, _cleanups, _vault = self._run(
            destroy_results=[True, True, False, False], sweeps=[empty, empty, absent]
        )

        assert ok is False
        assert failed == ["gco-global"]
        assert events.count("destroy gco-global") == 1

    def test_a_retried_delete_that_fails_again_is_reported(self):
        empty = {"status": "completed", "resource_status": "CREATE_COMPLETE", "errors": []}
        blocked = {"status": "completed", "resource_status": "DELETE_FAILED", "errors": []}
        (ok, _successful, failed), events, _cleanups, _vault = self._run(
            destroy_results=[True, True, False, False], sweeps=[empty, empty, blocked]
        )

        assert ok is False
        assert failed == ["gco-global"]
        assert events.count("destroy gco-global") == 2

    def test_strict_teardowns_fail_closed_before_the_global_delete(self):
        empty = {"status": "completed", "resource_status": "CREATE_COMPLETE", "errors": []}
        broken = {"status": "partial", "errors": [{"error": "AccessDeniedException"}]}
        with pytest.raises(
            RuntimeError, match="Strict backup-vault cleanup failed before deleting gco-global"
        ):
            self._run(destroy_results=[True, True], sweeps=[empty, broken], strict=True)

    def test_strict_sweeps_carry_the_checkpointed_global_identity(self):
        empty = {"status": "completed", "resource_status": "CREATE_COMPLETE", "errors": []}
        (ok, _successful, _failed), _events, _cleanups, vault = self._run(
            destroy_results=[True, True, True], sweeps=[empty, empty], strict=True
        )

        assert ok is True
        assert vault.call_count == 2
        for call_ in vault.call_args_list:
            assert call_.kwargs["expected_stack_id"] == "arn:gco-global"
            assert call_.kwargs["require_expected_identity"] is True
            assert call_.kwargs["authorize_stack"] is not None


class TestAnalyticsStackTeardownPhase:
    """``<project>-analytics`` is torn down with the global tier, never as a regional stack.

    Classified as a base regional stack it was deleted by keep-control-plane
    teardowns and ``gco upgrade`` (with Studio home directories and Cognito
    users), and every per-Region sweep derived the Region ``analytics`` from
    its name and called endpoints such as ``eks.analytics.amazonaws.com``.
    """

    _ALL_STACKS = [
        "gco-global",
        "gco-api-gateway",
        "gco-analytics",
        "gco-us-east-1",
        "gco-regional-api-us-east-1",
        "gco-monitoring",
    ]

    def _run(self, *, keep_control_plane: bool):
        from contextlib import ExitStack

        from cli.stacks import StackManager

        config = MagicMock()
        config.project_name = "gco"
        config.global_region = "us-east-2"
        destroyed: list[str] = []

        def fake_destroy(stack_name, **_kwargs):
            destroyed.append(stack_name)
            return True

        with ExitStack() as stack:
            stack.enter_context(
                patch.object(StackManager, "list_stacks", return_value=list(self._ALL_STACKS))
            )
            stack.enter_context(
                patch.object(StackManager, "detach_monitoring_from_regions", return_value=True)
            )
            watchdog = stack.enter_context(
                patch.object(StackManager, "_start_eks_sg_watchdog", return_value=MagicMock())
            )
            security_groups = stack.enter_context(
                patch.object(
                    StackManager,
                    "_cleanup_eks_security_groups",
                    return_value={"errors": [], "blocked_by_enis": []},
                )
            )
            stack.enter_context(
                patch.object(StackManager, "_destroy_phase_remaining_stacks", return_value=[])
            )
            stack.enter_context(patch.object(StackManager, "destroy", side_effect=fake_destroy))
            roles = stack.enter_context(
                patch.object(
                    StackManager,
                    "_detach_stack_roles_from_instance_profiles",
                    return_value={"detached": [], "errors": []},
                )
            )
            volumes = stack.enter_context(
                patch.object(
                    StackManager,
                    "_cleanup_cluster_volumes",
                    return_value={"deleted": [], "surviving": [], "errors": []},
                )
            )
            addons = stack.enter_context(
                patch.object(
                    StackManager,
                    "_cleanup_addon_parameters",
                    return_value={"deleted": [], "errors": []},
                )
            )
            result = StackManager(config).destroy_orchestrated(
                force=True, keep_control_plane=keep_control_plane
            )
        per_region_sweeps = {
            "watchdog": [call.args[0] for call in watchdog.call_args_list],
            "security_groups": [call.args[0] for call in security_groups.call_args_list],
            "roles": roles.call_args.args[0],
            "volumes": [call.args[0] for call in volumes.call_args_list],
            "addons": [call.args[0] for call in addons.call_args_list],
        }
        return result, destroyed, per_region_sweeps

    def test_full_teardown_deletes_it_after_the_api_gateway_and_before_global(self):
        (ok, _successful, failed), destroyed, sweeps = self._run(keep_control_plane=False)

        assert ok is True and failed == []
        # The API Gateway stack imports the analytics stack's Studio wiring and
        # both depend on the global stack, so no import-dropping redeploy is needed.
        assert destroyed == [
            "gco-monitoring",
            "gco-regional-api-us-east-1",
            "gco-us-east-1",
            "gco-api-gateway",
            "gco-analytics",
            "gco-global",
        ]
        assert sweeps == {
            "watchdog": ["gco-us-east-1"],
            "security_groups": ["gco-us-east-1"],
            "roles": ["gco-us-east-1"],
            "volumes": ["gco-us-east-1"],
            "addons": ["gco-us-east-1"],
        }

    def test_keep_control_plane_leaves_it_standing(self):
        (ok, successful, failed), destroyed, sweeps = self._run(keep_control_plane=True)

        assert ok is True and failed == []
        assert destroyed == ["gco-regional-api-us-east-1", "gco-us-east-1"]
        assert sorted(successful) == ["gco-regional-api-us-east-1", "gco-us-east-1"]
        assert sweeps["roles"] == ["gco-us-east-1"]
        assert sweeps["volumes"] == ["gco-us-east-1"]


class TestDetachMonitoringFromRegions:
    def _manager(self):
        from cli.stacks import StackManager

        config = MagicMock()
        config.project_name = "gco"
        return StackManager(config)

    def test_deploys_monitoring_with_the_control_plane_only_context_and_restores_it(self):
        from cli.stacks import StackManager

        manager = self._manager()
        manager.set_extra_cdk_context({"feature_enabled_overrides": "fsx_lustre"})
        seen_context: list[dict[str, str]] = []

        def fake_deploy(self, **kwargs):
            seen_context.append(dict(self._extra_cdk_context))
            return True

        with (
            patch.object(StackManager, "_stack_exists_in_cloudformation", return_value=True),
            patch.object(StackManager, "deploy", autospec=True, side_effect=fake_deploy) as deploy,
        ):
            assert manager.detach_monitoring_from_regions() is True

        deploy.assert_called_once()
        assert deploy.call_args.kwargs == {
            "stack_name": "gco-monitoring",
            "require_approval": False,
            "exclusively": True,
        }
        assert seen_context == [
            {"feature_enabled_overrides": "fsx_lustre", "gco:control-plane-only": "true"}
        ]
        # The caller's run-scoped context survives the detach untouched.
        assert manager._extra_cdk_context == {"feature_enabled_overrides": "fsx_lustre"}

    def test_context_is_restored_when_the_deploy_raises(self):
        from cli.stacks import StackManager

        manager = self._manager()
        with (
            patch.object(StackManager, "_stack_exists_in_cloudformation", return_value=True),
            patch.object(StackManager, "deploy", side_effect=RuntimeError("cdk failed")),
        ):
            with pytest.raises(RuntimeError) as excinfo:
                manager.detach_monitoring_from_regions()
            exc = excinfo.value
            assert str(exc) == "cdk failed"
        assert manager._extra_cdk_context == {}

    def test_an_absent_monitoring_stack_has_nothing_to_detach(self):
        from cli.stacks import StackManager

        manager = self._manager()
        with (
            patch.object(StackManager, "_stack_exists_in_cloudformation", return_value=False),
            patch.object(StackManager, "deploy") as deploy,
        ):
            assert manager.detach_monitoring_from_regions() is True
        deploy.assert_not_called()

    def test_a_failed_deploy_reports_false(self):
        from cli.stacks import StackManager

        manager = self._manager()
        with (
            patch.object(StackManager, "_stack_exists_in_cloudformation", return_value=True),
            patch.object(StackManager, "deploy", return_value=False),
        ):
            assert manager.detach_monitoring_from_regions() is False


class TestRoleInstanceProfileDetach:
    """Teardown removes a regional stack's own roles from instance profiles.

    IAM refuses to delete a role still in an instance profile, which left
    ``gco-<region>`` in DELETE_FAILED on NodeGroupRole during an upgrade.
    """

    @staticmethod
    def _manager():
        from cli.stacks import StackManager

        config = MagicMock()
        config.project_name = "gco"
        return StackManager.__new__(StackManager), config

    @staticmethod
    def _paginator(pages):
        paginator = MagicMock()
        paginator.paginate.return_value = pages
        return paginator

    def _run(self, cfn, iam, stacks):
        manager, config = self._manager()
        manager.config = config

        def client(service, **kwargs):
            if service == "cloudformation":
                assert kwargs["region_name"] in {"us-east-1", "us-west-2"}
                return cfn
            assert service == "iam"
            return iam

        with patch("boto3.client", side_effect=client):
            return manager._detach_stack_roles_from_instance_profiles(stacks)

    def test_detaches_only_live_stack_roles_and_keeps_the_profile(self, capsys) -> None:
        cfn = MagicMock()
        cfn.get_paginator.return_value = self._paginator(
            [
                {
                    "StackResourceSummaries": [
                        {
                            "ResourceType": "AWS::IAM::Role",
                            "PhysicalResourceId": "gco-us-east-1-NodeGroupRole-ABC",
                            "ResourceStatus": "CREATE_COMPLETE",
                        },
                        {
                            "ResourceType": "AWS::IAM::Role",
                            "PhysicalResourceId": "gco-us-east-1-GoneRole",
                            "ResourceStatus": "DELETE_COMPLETE",
                        },
                        {
                            "ResourceType": "AWS::S3::Bucket",
                            "PhysicalResourceId": "bucket",
                            "ResourceStatus": "CREATE_COMPLETE",
                        },
                        {"ResourceType": "AWS::IAM::Role", "ResourceStatus": "CREATE_FAILED"},
                    ]
                }
            ]
        )
        iam = MagicMock()
        iam.get_paginator.return_value = self._paginator(
            [{"InstanceProfiles": [{"InstanceProfileName": "external-profile"}]}]
        )

        outcome = self._run(cfn, iam, ["gco-us-east-1"])

        assert outcome == {
            "detached": [
                {
                    "stack": "gco-us-east-1",
                    "role": "gco-us-east-1-NodeGroupRole-ABC",
                    "instance_profile": "external-profile",
                }
            ],
            "errors": [],
        }
        iam.get_paginator.return_value.paginate.assert_called_once_with(
            RoleName="gco-us-east-1-NodeGroupRole-ABC"
        )
        iam.remove_role_from_instance_profile.assert_called_once_with(
            InstanceProfileName="external-profile", RoleName="gco-us-east-1-NodeGroupRole-ABC"
        )
        iam.delete_instance_profile.assert_not_called()
        assert "the profile is kept" in capsys.readouterr().out

    def test_missing_stacks_roles_and_profiles_are_quiet_and_other_errors_recorded(self) -> None:
        def cfn_error(code, message):
            return ClientError({"Error": {"Code": code, "Message": message}}, "ListStackResources")

        cfn = MagicMock()
        stack_pages = {
            "gco-us-east-1": cfn_error(
                "ValidationError", "Stack with id gco-us-east-1 does not exist"
            ),
            "gco-us-west-2": [
                {
                    "StackResourceSummaries": [
                        {
                            "ResourceType": "AWS::IAM::Role",
                            "PhysicalResourceId": name,
                            "ResourceStatus": "CREATE_COMPLETE",
                        }
                        for name in ("gone-role", "denied-role", "racing-role", "stuck-role")
                    ]
                }
            ],
        }

        def paginate(StackName):
            pages = stack_pages[StackName]
            if isinstance(pages, Exception):
                raise pages
            return pages

        cfn.get_paginator.return_value.paginate.side_effect = paginate

        def iam_error(code):
            return ClientError({"Error": {"Code": code, "Message": code}}, "op")

        def list_profiles(RoleName):
            if RoleName == "gone-role":
                raise iam_error("NoSuchEntity")
            if RoleName == "denied-role":
                raise iam_error("AccessDenied")
            return [{"InstanceProfiles": [{"InstanceProfileName": f"{RoleName}-profile"}]}]

        iam = MagicMock()
        iam.get_paginator.return_value.paginate.side_effect = list_profiles

        def remove(InstanceProfileName, RoleName):
            if RoleName == "racing-role":
                raise iam_error("NoSuchEntity")
            raise iam_error("LimitExceeded")

        iam.remove_role_from_instance_profile.side_effect = remove

        outcome = self._run(cfn, iam, ["gco-us-east-1", "gco-us-west-2"])

        assert outcome["detached"] == []
        assert len(outcome["errors"]) == 2
        assert outcome["errors"][0].startswith("denied-role: ")
        assert outcome["errors"][1].startswith("stuck-role in stuck-role-profile: ")

    def test_stack_lookup_failures_are_recorded_and_empty_input_is_a_no_op(self) -> None:
        manager, config = self._manager()
        manager.config = config
        with patch("boto3.client") as client:
            assert manager._detach_stack_roles_from_instance_profiles([]) == {
                "detached": [],
                "errors": [],
            }
        client.assert_not_called()

        cfn = MagicMock()
        cfn.get_paginator.return_value.paginate.side_effect = [
            ClientError({"Error": {"Code": "AccessDenied", "Message": "no"}}, "ListStackResources"),
            RuntimeError("boom"),
        ]
        outcome = self._run(cfn, MagicMock(), ["gco-us-east-1", "gco-us-west-2"])
        assert outcome["detached"] == []
        assert outcome["errors"][0].startswith("gco-us-east-1: ")
        assert outcome["errors"][1] == "gco-us-west-2: RuntimeError: boom"

    def test_stacks_that_name_no_region_are_skipped(self) -> None:
        """Only ``<project>-<region>`` names a Region; ``_run`` rejects any pseudo-Region."""
        cfn = MagicMock()
        cfn.get_paginator.return_value = self._paginator([{"StackResourceSummaries": []}])

        outcome = self._run(
            cfn,
            MagicMock(),
            ["gco-analytics", "gco-regional-api-us-east-1", "gco-global", "gco-us-east-1"],
        )

        assert outcome == {"detached": [], "errors": []}
        cfn.get_paginator.return_value.paginate.assert_called_once_with(StackName="gco-us-east-1")
