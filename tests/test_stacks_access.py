"""
Tests for `gco stacks access` — the kubectl bootstrap command in
cli/commands/stacks_cmd.py.

Covers region resolution (defaulting to the first entry of
cdk.json::context.regional and falling back to `-r`), cluster name
defaulting to `<project_name>-{region}`, and the EKS access-entry creation path:
translating `sts:assumed-role` ARNs to IAM role ARNs, invoking
`aws eks update-kubeconfig` and `create-access-entry` via patched
subprocess.run, and swallowing the "already exists" error on rerun
so the command is idempotent.
"""

from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from cli.commands.stacks_cmd import stacks


@pytest.fixture
def runner():
    return CliRunner()


class TestStacksAccessCommand:
    """Tests for gco stacks access."""

    def test_help_shows_usage(self, runner):
        result = runner.invoke(stacks, ["access", "--help"])
        assert result.exit_code == 0
        assert "Configure kubectl access" in result.output

    def test_default_region_from_cdk_json(self, runner):
        """Without -r, should read region from cdk.json."""
        with (
            patch("cli.config._load_cdk_json") as mock_cdk,
            patch("subprocess.run") as mock_run,
            patch("time.sleep"),
        ):
            mock_cdk.return_value = {"regional": ["us-east-1"]}
            mock_run.return_value = MagicMock(
                returncode=0, stdout="NAME STATUS\nnode1 Ready\n", stderr=""
            )

            runner.invoke(stacks, ["access"])
            # Should use us-east-1 from cdk.json
            calls = [str(c) for c in mock_run.call_args_list]
            assert any("us-east-1" in c for c in calls)

    def test_custom_region_override(self, runner):
        """With -r, should use the specified region."""
        with patch("subprocess.run") as mock_run, patch("time.sleep"):
            mock_run.return_value = MagicMock(
                returncode=0, stdout="NAME STATUS\nnode1 Ready\n", stderr=""
            )

            runner.invoke(stacks, ["access", "-r", "eu-west-1"])
            calls = [str(c) for c in mock_run.call_args_list]
            assert any("eu-west-1" in c for c in calls)

    def test_custom_cluster_name(self, runner):
        """With -c, should use the specified cluster name."""
        with patch("subprocess.run") as mock_run, patch("time.sleep"):
            mock_run.return_value = MagicMock(
                returncode=0, stdout="NAME STATUS\nnode1 Ready\n", stderr=""
            )

            runner.invoke(stacks, ["access", "-c", "my-cluster", "-r", "us-east-1"])
            calls = [str(c) for c in mock_run.call_args_list]
            assert any("my-cluster" in c for c in calls)

    def test_default_cluster_name_from_region(self, runner):
        """Without -c, cluster name should use the configured project and region."""
        with patch("subprocess.run") as mock_run, patch("time.sleep"):
            mock_run.return_value = MagicMock(
                returncode=0, stdout="NAME STATUS\nnode1 Ready\n", stderr=""
            )

            runner.invoke(stacks, ["access", "-r", "us-west-2"])
            calls = [str(c) for c in mock_run.call_args_list]
            assert any("gco-us-west-2" in c for c in calls)

    def test_handles_assumed_role(self, runner):
        """Should transform assumed-role ARN to IAM role ARN."""
        with patch("subprocess.run") as mock_run, patch("time.sleep"):
            # First call: update-kubeconfig (success)
            # Second call: get-caller-identity (returns assumed-role ARN)
            # Third call: get-caller-identity for account ID
            # Fourth call: create-access-entry
            # Fifth call: associate-access-policy
            # Sixth call: kubectl get nodes
            call_count = 0

            def side_effect(*args, **kwargs):
                nonlocal call_count
                call_count += 1
                cmd = args[0] if args else kwargs.get("args", [])
                cmd_str = " ".join(cmd) if isinstance(cmd, list) else str(cmd)

                mock = MagicMock(returncode=0, stderr="")
                if "get-caller-identity" in cmd_str and "Arn" in cmd_str:
                    mock.stdout = "arn:aws:sts::123456789012:assumed-role/MyRole/session\n"
                elif "get-caller-identity" in cmd_str and "Account" in cmd_str:
                    mock.stdout = "123456789012\n"
                elif "kubectl" in cmd_str:
                    mock.stdout = "NAME STATUS\nnode1 Ready\n"
                else:
                    mock.stdout = ""
                return mock

            mock_run.side_effect = side_effect

            runner.invoke(stacks, ["access", "-r", "us-east-1"])
            # Should have called create-access-entry with the role ARN
            calls = [str(c) for c in mock_run.call_args_list]
            assert any("arn:aws:iam::" in c for c in calls)

    @pytest.mark.parametrize(
        ("region", "partition"),
        (("cn-north-1", "aws-cn"), ("us-gov-west-1", "aws-us-gov")),
    )
    def test_access_arns_follow_target_partition(self, runner, region, partition):
        """Assumed-role and managed access-policy ARNs use Region metadata."""
        import json

        captured: list[list[str]] = []

        def side_effect(*args, **kwargs):
            cmd = args[0] if args else kwargs.get("args", [])
            if isinstance(cmd, list):
                captured.append(list(cmd))
            cmd_str = " ".join(cmd) if isinstance(cmd, list) else str(cmd)
            response = MagicMock(returncode=0, stderr="", stdout="")
            if "describe-cluster" in cmd_str:
                response.stdout = json.dumps({"public": True, "private": True, "publicCidrs": []})
            elif "get-caller-identity" in cmd_str and "Arn" in cmd_str:
                response.stdout = (
                    f"arn:{partition}:sts::123456789012:assumed-role/PartitionRole/session\n"
                )
            elif "get-caller-identity" in cmd_str and "Account" in cmd_str:
                response.stdout = "123456789012\n"
            elif "kubectl" in cmd_str:
                response.stdout = "NAME STATUS\nnode1 Ready\n"
            return response

        with patch("subprocess.run", side_effect=side_effect), patch("time.sleep"):
            result = runner.invoke(stacks, ["access", "-r", region])

        assert result.exit_code == 0, result.output
        create_call = next(cmd for cmd in captured if "create-access-entry" in cmd)
        assert (
            create_call[create_call.index("--principal-arn") + 1]
            == f"arn:{partition}:iam::123456789012:role/PartitionRole"
        )
        associate_call = next(cmd for cmd in captured if "associate-access-policy" in cmd)
        assert associate_call[associate_call.index("--policy-arn") + 1] == (
            f"arn:{partition}:eks::aws:cluster-access-policy/AmazonEKSClusterAdminPolicy"
        )

    def test_handles_existing_access_entry(self, runner):
        """Should handle 'already exists' gracefully."""
        import subprocess

        with patch("subprocess.run") as mock_run, patch("time.sleep"):

            def side_effect(*args, **kwargs):
                cmd = args[0] if args else kwargs.get("args", [])
                cmd_str = " ".join(cmd) if isinstance(cmd, list) else str(cmd)

                if "create-access-entry" in cmd_str:
                    raise subprocess.CalledProcessError(1, cmd, stderr="already exists")

                mock = MagicMock(returncode=0, stderr="")
                if "get-caller-identity" in cmd_str:
                    mock.stdout = "arn:aws:iam::123456789012:user/dev\n"
                elif "kubectl" in cmd_str:
                    mock.stdout = "NAME STATUS\nnode1 Ready\n"
                else:
                    mock.stdout = ""
                return mock

            mock_run.side_effect = side_effect

            result = runner.invoke(stacks, ["access", "-r", "us-east-1"])
            # Should not fail — handles existing entry gracefully
            assert "Access entry may already exist" in result.output


class TestStacksAccessEndpointWarning:
    """Endpoint-access warning surfaced when the cluster is private-only.

    When the EKS cluster's ``resourcesVpcConfig`` reports
    ``endpointPublicAccess=false``, ``gco stacks access`` still attempts the
    access-entry creation and policy association (those go through the EKS
    control plane via boto3, not through the cluster endpoint), but the
    final ``kubectl get nodes`` call cannot reach the API server from
    outside the VPC. The command surfaces a structured warning that
    points the operator at the ``cdk.json`` knob and the redeploy command.
    """

    def _patch_describe_endpoint(self, *, public: bool, public_cidrs: list[str] | None = None):
        """Build a ``subprocess.run`` side effect that returns the right
        cluster-endpoint payload for the describe-cluster call and the
        usual success exit codes for everything else, with kubectl
        reporting a network timeout to mimic a private-only cluster.
        """
        import json
        import subprocess

        def side_effect(*args, **kwargs):
            cmd = args[0] if args else kwargs.get("args", [])
            cmd_str = " ".join(cmd) if isinstance(cmd, list) else str(cmd)
            mock = MagicMock(returncode=0, stderr="")
            if "describe-cluster" in cmd_str and "resourcesVpcConfig" in cmd_str:
                mock.stdout = json.dumps(
                    {
                        "public": public,
                        "private": True,
                        "publicCidrs": public_cidrs or [],
                    }
                )
            elif "get-caller-identity" in cmd_str and "Arn" in cmd_str:
                mock.stdout = "arn:aws:iam::123456789012:user/dev\n"
            elif "kubectl" in cmd_str and not public:
                # Mimic the laptop-from-outside-the-VPC failure mode.
                mock.returncode = 1
                mock.stderr = "Unable to connect to the server: dial tcp 10.0.0.1:443: i/o timeout"
            elif "kubectl" in cmd_str:
                mock.stdout = "NAME STATUS\nnode1 Ready\n"
            elif "create-access-entry" in cmd_str:
                # Idempotent — pretend it already exists, exercising the
                # CalledProcessError path without affecting anything else.
                raise subprocess.CalledProcessError(1, cmd, stderr="already exists")
            return mock

        return side_effect

    def test_private_only_emits_actionable_warning(self, runner):
        with patch("subprocess.run") as mock_run, patch("time.sleep"):
            mock_run.side_effect = self._patch_describe_endpoint(public=False)
            result = runner.invoke(stacks, ["access", "-r", "us-east-1"])
        assert result.exit_code == 0, result.output
        # The early warning fires once on detection.
        assert "endpointPublicAccess=false" in result.output
        # The remediation hint surfaces at least once with the exact
        # cdk.json key the operator needs to flip.
        assert "PUBLIC_AND_PRIVATE" in result.output
        # And the redeploy command is named explicitly.
        assert "gco stacks deploy gco-us-east-1" in result.output

    def test_public_with_cidr_allowlist_notes_the_allowlist(self, runner):
        with patch("subprocess.run") as mock_run, patch("time.sleep"):
            mock_run.side_effect = self._patch_describe_endpoint(
                public=True, public_cidrs=["203.0.113.0/24"]
            )
            result = runner.invoke(stacks, ["access", "-r", "us-east-1"])
        assert result.exit_code == 0, result.output
        # We should mention the CIDR allowlist explicitly so the
        # operator can verify their egress IP is covered.
        assert "203.0.113.0/24" in result.output
        # And we shouldn't have also fired the private-only warning.
        assert "endpointPublicAccess=false" not in result.output

    def test_public_unrestricted_no_warning(self, runner):
        with patch("subprocess.run") as mock_run, patch("time.sleep"):
            mock_run.side_effect = self._patch_describe_endpoint(public=True)
            result = runner.invoke(stacks, ["access", "-r", "us-east-1"])
        assert result.exit_code == 0, result.output
        # No private-only or CIDR-allowlist warning should appear.
        assert "endpointPublicAccess=false" not in result.output
        assert "verify your egress IP" not in result.output


class TestStacksAccessEndpointWarningExtended:
    """Branch coverage for the endpoint-access probe and the network-error
    detector that backs the verify step.

    These complement ``TestStacksAccessEndpointWarning`` above by
    exercising the failure paths of the early ``describe-cluster``
    probe (so the access-entry steps still run when AWS is unreachable),
    the CIDR-allowlist info-line vs. the noisy private-only warning,
    and the kubectl-failure pattern matcher that decides between the
    structured remediation hint and the legacy 'no nodes found' fallback.
    """

    def test_describe_cluster_failure_does_not_block_setup(self, runner):
        """A failing describe-cluster step is logged but doesn't abort
        the access-entry creation flow.
        """
        import subprocess

        with patch("subprocess.run") as mock_run, patch("time.sleep"):

            def side_effect(*args, **kwargs):
                cmd = args[0] if args else kwargs.get("args", [])
                cmd_str = " ".join(cmd) if isinstance(cmd, list) else str(cmd)
                if "describe-cluster" in cmd_str:
                    raise subprocess.CalledProcessError(255, cmd, stderr="AccessDenied")
                mock = MagicMock(returncode=0, stderr="")
                if "get-caller-identity" in cmd_str:
                    mock.stdout = "arn:aws:iam::123456789012:user/dev\n"
                elif "kubectl" in cmd_str:
                    mock.stdout = "NAME STATUS\nnode1 Ready\n"
                else:
                    mock.stdout = ""
                return mock

            mock_run.side_effect = side_effect
            result = runner.invoke(stacks, ["access", "-r", "us-east-1"])

        assert result.exit_code == 0, result.output
        # The probe failure surfaces but doesn't block the remaining steps.
        assert "Could not determine endpoint access mode" in result.output
        # Access-entry path still proceeded — kubectl get nodes ran and succeeded.
        assert "Access configured successfully" in result.output

    def test_describe_cluster_filenotfound_handled(self, runner):
        """If the AWS CLI is missing, the probe step degrades gracefully."""
        with patch("subprocess.run") as mock_run, patch("time.sleep"):

            def side_effect(*args, **kwargs):
                cmd = args[0] if args else kwargs.get("args", [])
                cmd_str = " ".join(cmd) if isinstance(cmd, list) else str(cmd)
                if "describe-cluster" in cmd_str:
                    raise FileNotFoundError("aws not on PATH for describe-cluster")
                mock = MagicMock(returncode=0, stderr="")
                if "get-caller-identity" in cmd_str:
                    mock.stdout = "arn:aws:iam::123456789012:user/dev\n"
                elif "kubectl" in cmd_str:
                    mock.stdout = "NAME STATUS\nnode1 Ready\n"
                else:
                    mock.stdout = ""
                return mock

            mock_run.side_effect = side_effect
            result = runner.invoke(stacks, ["access", "-r", "us-east-1"])

        assert result.exit_code == 0, result.output
        assert "Could not determine endpoint access mode" in result.output

    def test_public_unrestricted_no_cidr_message(self, runner):
        """Public endpoint with empty publicCidrs is fully unrestricted —
        no allowlist info line, no warnings.
        """
        import json

        with patch("subprocess.run") as mock_run, patch("time.sleep"):

            def side_effect(*args, **kwargs):
                cmd = args[0] if args else kwargs.get("args", [])
                cmd_str = " ".join(cmd) if isinstance(cmd, list) else str(cmd)
                mock = MagicMock(returncode=0, stderr="")
                if "describe-cluster" in cmd_str and "resourcesVpcConfig" in cmd_str:
                    mock.stdout = json.dumps({"public": True, "private": True, "publicCidrs": []})
                elif "get-caller-identity" in cmd_str:
                    mock.stdout = "arn:aws:iam::123456789012:user/dev\n"
                elif "kubectl" in cmd_str:
                    mock.stdout = "NAME STATUS\nnode1 Ready\n"
                else:
                    mock.stdout = ""
                return mock

            mock_run.side_effect = side_effect
            result = runner.invoke(stacks, ["access", "-r", "us-east-1"])

        assert result.exit_code == 0, result.output
        # The CIDR allowlist info line should not appear.
        assert "verify your egress IP" not in result.output
        # And no remediation hint either, because the laptop reached the cluster.
        assert "PUBLIC_AND_PRIVATE" not in result.output
        assert "Access configured successfully" in result.output

    @pytest.mark.parametrize(
        "stderr_phrase",
        [
            "Unable to connect to the server: dial tcp 10.0.0.1:443: i/o timeout",
            "Unable to connect to the server: no route to host",
            "Unable to connect to the server: connection refused",
            "dial tcp 10.0.0.1:443: connect: operation timed out",
        ],
    )
    def test_kubectl_network_error_surfaces_remediation(self, runner, stderr_phrase):
        """Each of the four stderr phrases the matcher recognises should
        trip the structured remediation hint, not the legacy
        ``no nodes found`` line.
        """
        import json

        with patch("subprocess.run") as mock_run, patch("time.sleep"):

            def side_effect(*args, **kwargs):
                cmd = args[0] if args else kwargs.get("args", [])
                cmd_str = " ".join(cmd) if isinstance(cmd, list) else str(cmd)
                mock = MagicMock(returncode=0, stderr="")
                if "describe-cluster" in cmd_str and "resourcesVpcConfig" in cmd_str:
                    mock.stdout = json.dumps(
                        {
                            "public": True,
                            "private": True,
                            "publicCidrs": ["198.51.100.0/24"],
                        }
                    )
                elif "get-caller-identity" in cmd_str:
                    mock.stdout = "arn:aws:iam::123456789012:user/dev\n"
                elif "kubectl" in cmd_str:
                    mock.returncode = 1
                    mock.stderr = stderr_phrase
                else:
                    mock.stdout = ""
                return mock

            mock_run.side_effect = side_effect
            result = runner.invoke(stacks, ["access", "-r", "us-east-1"])

        assert result.exit_code == 0, result.output
        # The CIDR allowlist line surfaced because public=true with cidrs.
        assert "198.51.100.0/24" in result.output
        # And the remediation pointer fires because kubectl couldn't reach the API.
        assert "PUBLIC_AND_PRIVATE" in result.output
        assert "gco stacks deploy gco-us-east-1" in result.output

    def test_kubectl_non_network_failure_falls_back_to_legacy(self, runner):
        """When kubectl exits non-zero with a non-network stderr (e.g.
        permission denied), the fallback message is the legacy 'no
        nodes found' line — the structured hint would be misleading.
        """
        import json

        with patch("subprocess.run") as mock_run, patch("time.sleep"):

            def side_effect(*args, **kwargs):
                cmd = args[0] if args else kwargs.get("args", [])
                cmd_str = " ".join(cmd) if isinstance(cmd, list) else str(cmd)
                mock = MagicMock(returncode=0, stderr="")
                if "describe-cluster" in cmd_str and "resourcesVpcConfig" in cmd_str:
                    mock.stdout = json.dumps({"public": True, "private": True, "publicCidrs": []})
                elif "get-caller-identity" in cmd_str:
                    mock.stdout = "arn:aws:iam::123456789012:user/dev\n"
                elif "kubectl" in cmd_str:
                    mock.returncode = 1
                    mock.stderr = "Error from server (Forbidden): nodes is forbidden"
                else:
                    mock.stdout = ""
                return mock

            mock_run.side_effect = side_effect
            result = runner.invoke(stacks, ["access", "-r", "us-east-1"])

        assert result.exit_code == 0, result.output
        # Neither the CIDR-allowlist line nor the remediation pointer
        # should fire on a non-network kubectl failure.
        assert "PUBLIC_AND_PRIVATE" not in result.output
        # The legacy fallback line is what surfaces here.
        assert "no nodes found" in result.output

    def test_private_only_describes_query_shape_once(self, runner):
        """The describe-cluster probe must hit the right JMESPath query
        exactly once before the access-entry steps fire.
        """
        import json

        captured: list[list[str]] = []

        with patch("subprocess.run") as mock_run, patch("time.sleep"):

            def side_effect(*args, **kwargs):
                cmd = args[0] if args else kwargs.get("args", [])
                if isinstance(cmd, list):
                    captured.append(list(cmd))
                cmd_str = " ".join(cmd) if isinstance(cmd, list) else str(cmd)
                mock = MagicMock(returncode=0, stderr="")
                if "describe-cluster" in cmd_str and "resourcesVpcConfig" in cmd_str:
                    mock.stdout = json.dumps({"public": False, "private": True, "publicCidrs": []})
                elif "get-caller-identity" in cmd_str:
                    mock.stdout = "arn:aws:iam::123456789012:user/dev\n"
                elif "kubectl" in cmd_str:
                    mock.returncode = 1
                    mock.stderr = "dial tcp 10.0.0.1:443: i/o timeout"
                else:
                    mock.stdout = ""
                return mock

            mock_run.side_effect = side_effect
            runner.invoke(stacks, ["access", "-r", "us-east-1"])

        # Exactly one describe-cluster call, with the JMESPath we expect.
        describe_calls = [cmd for cmd in captured if "describe-cluster" in cmd and "--query" in cmd]
        assert len(describe_calls) == 1, describe_calls
        # The query string is one JMESPath expression — confirm it round-trips
        # through the explicit ``+`` that placates CodeQL.
        query_str = describe_calls[0][describe_calls[0].index("--query") + 1]
        assert "endpointPublicAccess" in query_str
        assert "endpointPrivateAccess" in query_str
        assert "publicAccessCidrs" in query_str
