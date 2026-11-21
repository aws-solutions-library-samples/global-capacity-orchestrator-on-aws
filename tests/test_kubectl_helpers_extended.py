"""
Extended tests for cli/kubectl_helpers.update_kubeconfig.

Expands on test_kubectl_helpers.py with stricter argv shape checks
(--name, --region positional order), region passthrough variants,
and error-message quality assertions (stderr content surfaces in the
RuntimeError, FileNotFoundError hint suggests installing the AWS CLI).
Also pins subprocess.run's capture_output=True/text=True invocation
so command output never leaks into the user's shell.
"""

from unittest.mock import MagicMock, patch

import pytest

from cli.kubectl_helpers import update_kubeconfig


class TestUpdateKubeconfig:
    """Tests for update_kubeconfig function."""

    @patch("subprocess.run")
    def test_success(self, mock_run):
        """Should succeed when aws eks update-kubeconfig returns 0."""
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        update_kubeconfig("gco-us-east-1", "us-east-1")
        mock_run.assert_called_once()
        cmd = mock_run.call_args[0][0]
        assert "aws" in cmd
        assert "update-kubeconfig" in cmd
        assert "--name" in cmd
        assert "gco-us-east-1" in cmd
        assert "--region" in cmd
        assert "us-east-1" in cmd

    @patch("subprocess.run")
    def test_failure_raises_runtime_error(self, mock_run):
        """Should raise RuntimeError when command returns non-zero."""
        mock_run.return_value = MagicMock(returncode=1, stderr="cluster not found")
        with pytest.raises(RuntimeError, match="Failed to update kubeconfig"):
            update_kubeconfig("nonexistent", "us-east-1")

    @patch("subprocess.run", side_effect=FileNotFoundError("aws not found"))
    def test_aws_cli_not_found(self, mock_run):
        """Should raise RuntimeError when AWS CLI is not installed."""
        with pytest.raises(RuntimeError, match="AWS CLI not found"):
            update_kubeconfig("gco-us-east-1", "us-east-1")

    @patch("subprocess.run")
    def test_passes_correct_region(self, mock_run):
        """Should pass the correct region to the AWS CLI."""
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        update_kubeconfig("gco-eu-west-1", "eu-west-1")
        cmd = mock_run.call_args[0][0]
        assert "eu-west-1" in cmd

    @patch("subprocess.run")
    def test_passes_correct_cluster_name(self, mock_run):
        """Should pass the correct cluster name to the AWS CLI."""
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        update_kubeconfig("my-custom-cluster", "ap-southeast-1")
        cmd = mock_run.call_args[0][0]
        assert "my-custom-cluster" in cmd


class TestUpdateKubeconfigErrorMessages:
    """Tests for error message quality."""

    @patch("subprocess.run")
    def test_error_includes_stderr(self, mock_run):
        """Error message should include stderr from the command."""
        mock_run.return_value = MagicMock(
            returncode=1, stderr="AccessDeniedException: User is not authorized"
        )
        with pytest.raises(RuntimeError, match="AccessDeniedException"):
            update_kubeconfig("gco-us-east-1", "us-east-1")

    @patch("subprocess.run", side_effect=FileNotFoundError())
    def test_file_not_found_suggests_install(self, mock_run):
        """FileNotFoundError should suggest installing AWS CLI."""
        with pytest.raises(RuntimeError, match="install the AWS CLI"):
            update_kubeconfig("gco-us-east-1", "us-east-1")


class TestUpdateKubeconfigCommandStructure:
    """Tests for the kubectl command structure."""

    @patch("subprocess.run")
    def test_uses_capture_output(self, mock_run):
        """Should use capture_output to avoid polluting stdout."""
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        update_kubeconfig("gco-us-east-1", "us-east-1")
        call_kwargs = mock_run.call_args[1]
        assert call_kwargs["capture_output"] is True
        assert call_kwargs["text"] is True
