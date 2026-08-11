"""
Tests for cli/kubectl_helpers.update_kubeconfig.

Drives the thin wrapper around `aws eks update-kubeconfig` against a
patched subprocess.run: success with correct argv shape, non-zero
return codes surfaced as RuntimeError, CalledProcessError handling,
and the friendly "AWS CLI not found" message when the binary is missing.
"""

import subprocess
from unittest.mock import MagicMock, patch

import pytest

from cli.kubectl_helpers import update_kubeconfig


class TestUpdateKubeconfig:
    """Tests for update_kubeconfig helper."""

    def test_update_kubeconfig_success(self):
        """Test successful kubeconfig update."""
        with patch("cli.kubectl_helpers.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stderr="")
            update_kubeconfig("my-cluster", "us-east-1")

            mock_run.assert_called_once()
            cmd = mock_run.call_args[0][0]
            assert "update-kubeconfig" in cmd
            assert "--name" in cmd
            assert "my-cluster" in cmd
            assert "--region" in cmd
            assert "us-east-1" in cmd

    def test_update_kubeconfig_failure(self):
        """Test kubeconfig update failure raises RuntimeError."""
        with patch("cli.kubectl_helpers.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stderr="Cluster not found")
            with pytest.raises(RuntimeError, match="Failed to update kubeconfig"):
                update_kubeconfig("bad-cluster", "us-east-1")

    def test_update_kubeconfig_called_process_error(self):
        """Test kubeconfig update handles CalledProcessError."""
        with patch("cli.kubectl_helpers.subprocess.run") as mock_run:
            mock_run.side_effect = subprocess.CalledProcessError(
                1, "aws", stderr="Cluster not found"
            )
            with pytest.raises(RuntimeError, match="Failed to update kubeconfig"):
                update_kubeconfig("bad-cluster", "us-east-1")

    def test_update_kubeconfig_aws_cli_not_found(self):
        """Test kubeconfig update when AWS CLI is not installed."""
        with patch("cli.kubectl_helpers.subprocess.run") as mock_run:
            mock_run.side_effect = FileNotFoundError()
            with pytest.raises(RuntimeError, match="AWS CLI not found"):
                update_kubeconfig("my-cluster", "us-east-1")


class TestTunnelPreservation:
    """An active local-tunnel kubeconfig pin must survive update_kubeconfig.

    Live regression (example-job validation run ex241-66c02e71): the harness
    pinned kubeconfig at an SSM tunnel; ``gco jobs submit-direct`` then ran
    ``update_kubeconfig``, which rewrote the server back to the private EKS
    endpoint and every subsequent kubectl call timed out.
    """

    @staticmethod
    def _write_kubeconfig(tmp_path, server: str, *, tls_server_name: str | None) -> str:
        import yaml

        cluster: dict = {"server": server}
        if tls_server_name:
            cluster["tls-server-name"] = tls_server_name
        path = tmp_path / "config"
        path.write_text(
            yaml.safe_dump(
                {
                    "clusters": [
                        {
                            "name": "arn:aws:eks:us-east-1:123456789012:cluster/my-cluster",
                            "cluster": cluster,
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        return str(path)

    def test_tunnel_pinned_entry_is_preserved(self, tmp_path, monkeypatch):
        monkeypatch.setenv(
            "KUBECONFIG",
            self._write_kubeconfig(
                tmp_path,
                "https://localhost:8443",
                tls_server_name="abc123.gr7.us-east-1.eks.amazonaws.com",
            ),
        )
        with patch("cli.kubectl_helpers.subprocess.run") as mock_run:
            update_kubeconfig("my-cluster", "us-east-1")
        mock_run.assert_not_called()

    def test_real_endpoint_entry_is_refreshed(self, tmp_path, monkeypatch):
        monkeypatch.setenv(
            "KUBECONFIG",
            self._write_kubeconfig(
                tmp_path,
                "https://abc123.gr7.us-east-1.eks.amazonaws.com",
                tls_server_name=None,
            ),
        )
        with patch("cli.kubectl_helpers.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stderr="")
            update_kubeconfig("my-cluster", "us-east-1")
        mock_run.assert_called_once()

    def test_localhost_without_tls_pin_is_refreshed(self, tmp_path, monkeypatch):
        # Only the deliberate tunnel shape (localhost + tls-server-name) is
        # preserved; a bare localhost server is not treated as a tunnel.
        monkeypatch.setenv(
            "KUBECONFIG",
            self._write_kubeconfig(tmp_path, "https://localhost:8443", tls_server_name=None),
        )
        with patch("cli.kubectl_helpers.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stderr="")
            update_kubeconfig("my-cluster", "us-east-1")
        mock_run.assert_called_once()

    def test_other_clusters_tunnel_does_not_block_refresh(self, tmp_path, monkeypatch):
        monkeypatch.setenv(
            "KUBECONFIG",
            self._write_kubeconfig(
                tmp_path,
                "https://localhost:8443",
                tls_server_name="abc123.gr7.us-east-1.eks.amazonaws.com",
            ),
        )
        with patch("cli.kubectl_helpers.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stderr="")
            update_kubeconfig("other-cluster", "us-east-1")
        mock_run.assert_called_once()

    def test_missing_kubeconfig_is_refreshed(self, tmp_path, monkeypatch):
        monkeypatch.setenv("KUBECONFIG", str(tmp_path / "does-not-exist"))
        with patch("cli.kubectl_helpers.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stderr="")
            update_kubeconfig("my-cluster", "us-east-1")
        mock_run.assert_called_once()
