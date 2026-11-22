"""
Tests for cli/kubectl_helpers.update_kubeconfig.

Drives the thin wrapper around `aws eks update-kubeconfig` against a
patched subprocess.run: success with correct argv shape, non-zero
return codes surfaced as RuntimeError, CalledProcessError handling,
and the friendly "AWS CLI not found" message when the binary is missing.
"""

import os
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from cli.kubectl_helpers import update_kubeconfig


class TestUpdateKubeconfigInputValidation:
    """update_kubeconfig validates before touching the filesystem or AWS CLI."""

    def test_rejects_invalid_cluster_name(self):
        with (
            patch("cli.kubectl_helpers.subprocess.run") as mock_run,
            pytest.raises(ValueError, match="Invalid cluster name"),
        ):
            update_kubeconfig("bad cluster name!", "us-east-1")
        mock_run.assert_not_called()

    def test_rejects_invalid_region(self):
        with (
            patch("cli.kubectl_helpers.subprocess.run") as mock_run,
            pytest.raises(ValueError, match="Invalid AWS region"),
        ):
            update_kubeconfig("my-cluster", "not-a-region")
        mock_run.assert_not_called()


class TestUpdateKubeconfig:
    """Tests for update_kubeconfig helper."""

    def test_update_kubeconfig_success(self):
        """Test successful kubeconfig update."""
        with patch("cli.kubectl_helpers.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
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
        name = "arn:aws:eks:us-east-1:123456789012:cluster/my-cluster"
        path = tmp_path / "config"
        path.write_text(
            yaml.safe_dump(
                {
                    "clusters": [{"name": name, "cluster": cluster}],
                    "contexts": [{"name": name, "context": {"cluster": name, "user": name}}],
                    "current-context": name,
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
            context = update_kubeconfig("my-cluster", "us-east-1")
        mock_run.assert_not_called()
        assert context == "arn:aws:eks:us-east-1:123456789012:cluster/my-cluster"

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
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
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
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
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
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            update_kubeconfig("other-cluster", "us-east-1")
        mock_run.assert_called_once()

    def test_missing_kubeconfig_is_refreshed(self, tmp_path, monkeypatch):
        monkeypatch.setenv("KUBECONFIG", str(tmp_path / "does-not-exist"))
        with patch("cli.kubectl_helpers.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            update_kubeconfig("my-cluster", "us-east-1")
        mock_run.assert_called_once()

    def test_kubeconfig_env_with_leading_empty_path_falls_back_to_default(
        self, tmp_path, monkeypatch
    ):
        """A ``KUBECONFIG`` whose first ``:``-separated entry is empty (e.g.
        ``:/other/path``) must fall back to the default ``~/.kube/config``
        location rather than treating the empty string as a real path."""
        monkeypatch.setenv("KUBECONFIG", f"{os.pathsep}{tmp_path / 'other'}")
        monkeypatch.setenv("HOME", str(tmp_path))
        with patch("cli.kubectl_helpers.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            update_kubeconfig("my-cluster", "us-east-1")
        mock_run.assert_called_once()

    def test_unset_kubeconfig_env_means_the_default_file(self, tmp_path, monkeypatch):
        """With ``KUBECONFIG`` unset entirely the default ``~/.kube/config`` is
        used. The suite's autouse fixture pins ``KUBECONFIG`` for every test,
        so this is the one place the unset branch is exercised."""
        from cli.kubectl_helpers import _kubeconfig_file

        monkeypatch.delenv("KUBECONFIG", raising=False)
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
        assert _kubeconfig_file() == tmp_path / ".kube" / "config"


class TestKubeconfigContext:
    """update_kubeconfig names the context every later kubectl call must use.

    With a tunnel-pinned entry the refresh is skipped, so the current context
    is whatever the operator (or a refresh of another cluster) last selected.
    kubectl calls without ``--context`` then reached that other cluster.
    """

    _ARN = "arn:aws:eks:us-east-1:123456789012:cluster/my-cluster"
    _OTHER_ARN = "arn:aws:eks:us-west-2:123456789012:cluster/other-cluster"

    @classmethod
    def _write(cls, tmp_path, monkeypatch, *, contexts, current) -> None:
        import yaml

        path = tmp_path / "config"
        path.write_text(
            yaml.safe_dump(
                {
                    "clusters": [
                        {
                            "name": cls._OTHER_ARN,
                            "cluster": {"server": "https://other.example.com"},
                        },
                        {
                            "name": cls._ARN,
                            "cluster": {
                                "server": "https://127.0.0.1:8443",
                                "tls-server-name": "abc123.gr7.us-east-1.eks.amazonaws.com",
                            },
                        },
                    ],
                    "contexts": contexts,
                    "current-context": current,
                }
            ),
            encoding="utf-8",
        )
        monkeypatch.setenv("KUBECONFIG", str(path))

    def test_pinned_entry_returns_its_context_not_the_current_one(self, tmp_path, monkeypatch):
        self._write(
            tmp_path,
            monkeypatch,
            contexts=[
                {"name": self._OTHER_ARN, "context": {"cluster": self._OTHER_ARN}},
                {"name": self._ARN, "context": {"cluster": self._ARN, "user": self._ARN}},
            ],
            current=self._OTHER_ARN,
        )
        with patch("cli.kubectl_helpers.subprocess.run") as mock_run:
            assert update_kubeconfig("my-cluster", "us-east-1") == self._ARN
        mock_run.assert_not_called()

    def test_pinned_entry_prefers_the_current_context_among_its_own(self, tmp_path, monkeypatch):
        self._write(
            tmp_path,
            monkeypatch,
            contexts=[
                {"name": self._ARN, "context": {"cluster": self._ARN}},
                {"name": "my-alias", "context": {"cluster": self._ARN}},
            ],
            current="my-alias",
        )
        assert update_kubeconfig("my-cluster", "us-east-1") == "my-alias"

    def test_pinned_entry_without_a_context_is_an_error(self, tmp_path, monkeypatch):
        self._write(
            tmp_path,
            monkeypatch,
            contexts=[
                "not-a-mapping",
                {"name": self._OTHER_ARN, "context": {"cluster": self._OTHER_ARN}},
                {"name": "", "context": {"cluster": self._ARN}},
                {"name": "no-body"},
            ],
            current=self._OTHER_ARN,
        )
        with (
            patch("cli.kubectl_helpers.subprocess.run") as mock_run,
            pytest.raises(RuntimeError, match="no kubeconfig context uses it"),
        ):
            update_kubeconfig("my-cluster", "us-east-1")
        mock_run.assert_not_called()

    def test_refresh_returns_the_context_the_aws_cli_wrote(self):
        with patch("cli.kubectl_helpers.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout=f"Added new context {self._ARN} to /home/user/.kube/my config\n",
                stderr="",
            )
            assert update_kubeconfig("my-cluster", "us-east-1") == self._ARN
            mock_run.return_value.stdout = f"Updated context {self._ARN} in /tmp/kube\n"
            assert update_kubeconfig("my-cluster", "us-east-1") == self._ARN

    def test_refresh_without_a_reported_context_leaves_kubectl_on_the_current_one(self):
        with patch("cli.kubectl_helpers.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            assert update_kubeconfig("my-cluster", "us-east-1") is None

    def test_malformed_kubeconfig_documents_are_not_pins(self, tmp_path, monkeypatch):
        path = tmp_path / "config"
        monkeypatch.setenv("KUBECONFIG", str(path))
        for document in ("- just\n- a list\n", "clusters:\n  - not-a-mapping\n"):
            path.write_text(document, encoding="utf-8")
            with patch("cli.kubectl_helpers.subprocess.run") as mock_run:
                mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
                assert update_kubeconfig("my-cluster", "us-east-1") is None
            mock_run.assert_called_once()

    def test_context_flags(self):
        from cli.kubectl_helpers import kubectl_context_flags

        assert kubectl_context_flags(None) == []
        assert kubectl_context_flags(self._ARN) == [f"--context={self._ARN}"]


@pytest.mark.parametrize(
    ("stderr", "expected"),
    [
        ("Unable to connect to the server: dial tcp 10.0.1.5:443: i/o timeout", True),
        ("Unable to connect to the server: net/http: TLS handshake timeout", True),
        ("dial tcp: lookup ABC.gr7.us-east-1.eks.amazonaws.com: no such host", True),
        ('Error from server (Forbidden): jobs.batch "x" is forbidden', False),
        ("error: You must be logged in to the server (Unauthorized)", False),
    ],
)
def test_kubectl_could_not_connect_tells_reachability_from_refusal(stderr, expected):
    """Only failures that never reached the API server count as unreachable."""
    from cli.kubectl_helpers import kubectl_could_not_connect

    assert kubectl_could_not_connect(stderr) is expected
