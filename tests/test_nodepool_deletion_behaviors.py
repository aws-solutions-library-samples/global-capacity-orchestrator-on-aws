"""NodePool client-lifecycle and deletion behavior tests."""

from __future__ import annotations

import base64
import gc
import os
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from cli.commands.nodepools_cmd import nodepools
from cli.config import GCOConfig
from cli.nodepools import (
    delete_cluster_nodepool,
    describe_cluster_nodepool,
    get_k8s_client,
    list_cluster_nodepools,
)


def _cluster_response() -> dict:
    return {
        "cluster": {
            "endpoint": "https://cluster.example",
            "certificateAuthority": {"data": base64.b64encode(b"test-ca").decode()},
        }
    }


def _config() -> GCOConfig:
    return GCOConfig(project_name="test-gco", default_region="us-east-1")


def test_k8s_client_closes_only_a_descriptor_it_still_owns() -> None:
    eks = MagicMock()
    eks.describe_cluster.return_value = _cluster_response()
    original = RuntimeError("fdopen failed before ownership transfer")
    with (
        patch("cli.nodepools.boto3.client", return_value=eks),
        patch("cli.nodepools.tempfile.mkstemp", return_value=(123, "/tmp/gco-ca-test.crt")),
        patch("cli.nodepools.os.fdopen", side_effect=original),
        patch("cli.nodepools.os.close", side_effect=OSError("already closed")) as close_fd,
        patch("cli.nodepools._unlink_temp_ca_cert") as unlink,
        pytest.raises(RuntimeError, match="fdopen failed") as exc_info,
    ):
        get_k8s_client("cluster", "us-east-1")

    assert exc_info.value is original
    close_fd.assert_called_once_with(123)
    unlink.assert_called_once_with("/tmp/gco-ca-test.crt")


def test_k8s_client_write_failure_does_not_double_close_transferred_descriptor() -> None:
    eks = MagicMock()
    eks.describe_cluster.return_value = _cluster_response()
    ca_file = MagicMock()
    ca_file.__enter__.return_value = ca_file
    ca_file.write.side_effect = RuntimeError("write failed")
    with (
        patch("cli.nodepools.boto3.client", return_value=eks),
        patch("cli.nodepools.tempfile.mkstemp", return_value=(456, "/tmp/gco-ca-write.crt")),
        patch("cli.nodepools.os.fdopen", return_value=ca_file),
        patch("cli.nodepools.os.close") as close_fd,
        patch("cli.nodepools._unlink_temp_ca_cert") as unlink,
        pytest.raises(RuntimeError, match="write failed"),
    ):
        get_k8s_client("cluster", "us-east-1")

    close_fd.assert_not_called()
    unlink.assert_called_once_with("/tmp/gco-ca-write.crt")


def test_k8s_client_removes_ca_file_when_api_client_construction_fails(tmp_path) -> None:
    eks = MagicMock()
    eks.describe_cluster.return_value = _cluster_response()
    ca_path = tmp_path / "construction-failure.crt"
    fd = os.open(ca_path, os.O_RDWR | os.O_CREAT | os.O_TRUNC, 0o600)
    with (
        patch("cli.nodepools.boto3.client", return_value=eks),
        patch("cli.nodepools.tempfile.mkstemp", return_value=(fd, str(ca_path))),
        patch("cli.nodepools.get_eks_token", return_value="token"),
        patch("kubernetes.client.ApiClient", side_effect=RuntimeError("client failed")),
        pytest.raises(RuntimeError, match="client failed"),
    ):
        get_k8s_client("cluster", "us-east-1")

    assert not ca_path.exists()


def test_k8s_client_ca_file_lifetime_matches_owning_api_client(tmp_path) -> None:
    eks = MagicMock()
    eks.describe_cluster.return_value = _cluster_response()
    ca_path = tmp_path / "owned-ca.crt"
    fd = os.open(ca_path, os.O_RDWR | os.O_CREAT | os.O_TRUNC, 0o600)

    class FakeConfiguration:
        pass

    class FakeApiClient:
        def __init__(self, configuration):
            self.configuration = configuration

        def close(self):
            return None

    class FakeCustomObjectsApi:
        def __init__(self, api_client):
            self.api_client = api_client

    with (
        patch("cli.nodepools.boto3.client", return_value=eks),
        patch("cli.nodepools.tempfile.mkstemp", return_value=(fd, str(ca_path))),
        patch("cli.nodepools.get_eks_token", return_value="token"),
        patch("kubernetes.client.Configuration", FakeConfiguration),
        patch("kubernetes.client.ApiClient", FakeApiClient),
        patch("kubernetes.client.CustomObjectsApi", FakeCustomObjectsApi),
    ):
        custom_api = get_k8s_client("cluster", "us-east-1")

    api_client = custom_api.api_client
    assert ca_path.exists()
    assert api_client.configuration.ssl_ca_cert == str(ca_path)
    del custom_api
    gc.collect()
    assert ca_path.exists()
    del api_client
    gc.collect()
    assert not ca_path.exists()


def test_list_nodepools_continues_after_an_instance_type_requirement() -> None:
    api = MagicMock()
    api.list_cluster_custom_object.return_value = {
        "items": [
            {
                "metadata": {"name": "gpu"},
                "spec": {
                    "template": {
                        "spec": {
                            "requirements": [
                                {
                                    "key": "topology.kubernetes.io/zone",
                                    "values": ["us-east-1a"],
                                },
                                {
                                    "key": "node.kubernetes.io/instance-type",
                                    "values": ["g5.xlarge"],
                                },
                                {"key": "karpenter.sh/capacity-type", "values": ["spot"]},
                            ]
                        }
                    }
                },
                "status": {"conditions": []},
            }
        ]
    }
    with patch("cli.nodepools.get_k8s_client", return_value=api):
        result = list_cluster_nodepools("cluster", "us-east-1")

    assert result[0]["instance_types"] == "g5.xlarge"
    assert result[0]["capacity_types"] == "spot"


def test_describe_nodepool_returns_none_for_a_non_mapping_response() -> None:
    api = MagicMock()
    api.get_cluster_custom_object.return_value = ["unexpected"]
    with patch("cli.nodepools.get_k8s_client", return_value=api):
        assert describe_cluster_nodepool("cluster", "us-east-1", "gpu") is None


def test_delete_nodepool_removes_the_paired_nodeclass() -> None:
    api = MagicMock()
    with patch("cli.nodepools.get_k8s_client", return_value=api):
        result = delete_cluster_nodepool("cluster", "us-east-1", "gpu")

    assert result == {"nodepool": "gpu", "ec2nodeclass": "gpu-nodeclass"}
    assert api.delete_cluster_custom_object.call_count == 2


def test_delete_nodepool_ignores_a_missing_paired_nodeclass() -> None:
    api = MagicMock()
    api.delete_cluster_custom_object.side_effect = [None, RuntimeError("404 Not Found")]
    with patch("cli.nodepools.get_k8s_client", return_value=api):
        result = delete_cluster_nodepool("cluster", "us-east-1", "gpu")

    assert result == {"nodepool": "gpu", "ec2nodeclass": None}


def test_delete_nodepool_warns_but_succeeds_when_nodeclass_cleanup_fails(caplog) -> None:
    api = MagicMock()
    api.delete_cluster_custom_object.side_effect = [None, RuntimeError("500 unavailable")]
    with patch("cli.nodepools.get_k8s_client", return_value=api):
        result = delete_cluster_nodepool("cluster", "us-east-1", "gpu")

    assert result["nodepool"] == "gpu"
    assert result["ec2nodeclass"] is None
    assert "Could not delete EC2NodeClass" in caplog.text


def test_delete_nodepool_wraps_primary_deletion_failures() -> None:
    api = MagicMock()
    api.delete_cluster_custom_object.side_effect = RuntimeError("forbidden")
    with (
        patch("cli.nodepools.get_k8s_client", return_value=api),
        pytest.raises(RuntimeError, match="Failed to delete NodePool: forbidden"),
    ):
        delete_cluster_nodepool("cluster", "us-east-1", "gpu")


def test_delete_cli_confirmation_reports_the_paired_nodeclass() -> None:
    with patch(
        "cli.nodepools.delete_cluster_nodepool",
        return_value={"nodepool": "gpu", "ec2nodeclass": "gpu-nodeclass"},
    ) as delete:
        result = CliRunner().invoke(
            nodepools,
            ["delete", "gpu", "--region", "us-east-1"],
            input="y\n",
            obj=_config(),
        )

    assert result.exit_code == 0, result.output
    assert "Delete NodePool 'gpu'" in result.output
    assert "Deleted EC2NodeClass gpu-nodeclass" in result.output
    delete.assert_called_once_with("test-gco-us-east-1", "us-east-1", "gpu")


def test_delete_cli_yes_bypasses_confirmation_and_handles_no_nodeclass() -> None:
    with patch(
        "cli.nodepools.delete_cluster_nodepool",
        return_value={"nodepool": "gpu", "ec2nodeclass": None},
    ):
        result = CliRunner().invoke(
            nodepools,
            ["delete", "gpu", "--region", "us-east-1", "--cluster", "custom", "--yes"],
            obj=_config(),
        )

    assert result.exit_code == 0, result.output
    assert "Delete NodePool 'gpu'" not in result.output
    assert "Deleted NodePool gpu" in result.output
    assert "EC2NodeClass" not in result.output


def test_delete_cli_surfaces_service_errors() -> None:
    with patch(
        "cli.nodepools.delete_cluster_nodepool", side_effect=RuntimeError("cluster unavailable")
    ):
        result = CliRunner().invoke(
            nodepools,
            ["delete", "gpu", "--region", "us-east-1", "--yes"],
            obj=_config(),
        )

    assert result.exit_code == 1
    assert "cluster unavailable" in result.output


def test_temp_ca_cleanup_tolerates_absent_and_unremovable_paths(tmp_path, caplog) -> None:
    from cli.nodepools import _unlink_temp_ca_cert

    caplog.set_level("DEBUG", logger="cli.nodepools")
    _unlink_temp_ca_cert(str(tmp_path / "already-gone.crt"))
    with patch("cli.nodepools.os.unlink", side_effect=PermissionError("busy")):
        _unlink_temp_ca_cert(str(tmp_path / "busy.crt"))

    assert "Failed to remove temporary Kubernetes CA certificate" in caplog.text


def test_k8s_client_closes_api_client_if_custom_api_construction_fails(tmp_path) -> None:
    eks = MagicMock()
    eks.describe_cluster.return_value = _cluster_response()
    ca_path = tmp_path / "custom-api-failure.crt"
    fd = os.open(ca_path, os.O_RDWR | os.O_CREAT | os.O_TRUNC, 0o600)
    api_client = MagicMock()
    with (
        patch("cli.nodepools.boto3.client", return_value=eks),
        patch("cli.nodepools.tempfile.mkstemp", return_value=(fd, str(ca_path))),
        patch("cli.nodepools.get_eks_token", return_value="token"),
        patch("kubernetes.client.ApiClient", return_value=api_client),
        patch(
            "kubernetes.client.CustomObjectsApi",
            side_effect=RuntimeError("custom API failed"),
        ),
        pytest.raises(RuntimeError, match="custom API failed"),
    ):
        get_k8s_client("cluster", "us-east-1")

    api_client.close.assert_called_once_with()
    assert not ca_path.exists()
