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

    def test_none_status_returns_none_without_sleeping(self):
        from cli.stacks import StackManager

        manager = self._manager()
        with (
            patch.object(StackManager, "_get_stack_status", return_value=None),
            patch("time.sleep") as mock_sleep,
        ):
            assert manager._wait_for_stack_settle("gco-monitoring") is None
            mock_sleep.assert_not_called()

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
