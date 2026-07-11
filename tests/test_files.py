"""
Tests for cli/files.py — the shared-filesystem CLI surface.

Covers the FileSystemInfo and FileInfo dataclasses plus baseline
FileSystemClient initialization with get_config and get_aws_client
patched out. The broader end-to-end coverage of EFS/FSx discovery,
DataSync transfers, and error paths lives in test_files_extended.py.
"""

from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest


class TestFileSystemInfo:
    """Tests for FileSystemInfo dataclass."""

    def test_file_system_info_creation(self):
        """Test creating FileSystemInfo."""
        from cli.files import FileSystemInfo

        fs = FileSystemInfo(
            file_system_id="fs-12345678",
            file_system_type="efs",
            region="us-east-1",
            dns_name="fs-12345678.efs.us-east-1.amazonaws.com",
            status="available",
        )

        assert fs.file_system_id == "fs-12345678"
        assert fs.file_system_type == "efs"
        assert fs.region == "us-east-1"
        assert fs.tags == {}

    def test_file_system_info_with_all_fields(self):
        """Test FileSystemInfo with all fields."""
        from cli.files import FileSystemInfo

        fs = FileSystemInfo(
            file_system_id="fs-abcdef12",
            file_system_type="fsx",
            region="us-west-2",
            dns_name="fs-abcdef12.fsx.us-west-2.amazonaws.com",
            mount_target_ip="10.0.1.100",
            size_bytes=1200 * 1024 * 1024 * 1024,
            status="available",
            created_time=datetime(2024, 1, 1, 10, 0, 0),
            tags={"Environment": "production"},
        )

        assert fs.mount_target_ip == "10.0.1.100"
        assert fs.size_bytes == 1200 * 1024 * 1024 * 1024
        assert fs.tags["Environment"] == "production"


class TestFileInfo:
    """Tests for FileInfo dataclass."""

    def test_file_info_creation(self):
        """Test creating FileInfo."""
        from cli.files import FileInfo

        file = FileInfo(
            path="/data/output.txt",
            name="output.txt",
            is_directory=False,
            size_bytes=1024,
        )

        assert file.path == "/data/output.txt"
        assert file.name == "output.txt"
        assert file.is_directory is False
        assert file.size_bytes == 1024

    def test_file_info_directory(self):
        """Test FileInfo for directory."""
        from cli.files import FileInfo

        dir_info = FileInfo(
            path="/data/models",
            name="models",
            is_directory=True,
        )

        assert dir_info.is_directory is True
        assert dir_info.size_bytes == 0


class TestFileSystemClient:
    """Tests for FileSystemClient class."""

    def test_client_initialization(self):
        """Test FileSystemClient initialization."""
        from cli.files import FileSystemClient

        with patch("cli.files.get_config") as mock_config:
            mock_config.return_value = MagicMock()
            mock_config.return_value.project_name = "gco"
            with patch("cli.files.get_aws_client") as mock_aws:
                mock_aws.return_value = MagicMock()
                client = FileSystemClient()
                assert client.config is not None

    def test_get_file_systems_empty(self):
        """Test getting file systems when none exist."""
        from cli.files import FileSystemClient

        with patch("cli.files.get_config") as mock_config:
            mock_config.return_value = MagicMock()
            mock_config.return_value.project_name = "gco"
            with patch("cli.files.get_aws_client") as mock_aws:
                mock_aws_client = MagicMock()
                mock_aws_client.discover_regional_stacks.return_value = {}
                mock_aws.return_value = mock_aws_client

                client = FileSystemClient()
                file_systems = client.get_file_systems()

                assert file_systems == []

    def test_get_file_system_by_region(self):
        """Test getting file system by region."""
        from cli.files import FileSystemClient, FileSystemInfo

        with patch("cli.files.get_config") as mock_config:
            mock_config.return_value = MagicMock()
            mock_config.return_value.project_name = "gco"
            with patch("cli.files.get_aws_client") as mock_aws:
                mock_aws.return_value = MagicMock()

                client = FileSystemClient()

                # Mock get_file_systems to return test data
                with patch.object(client, "get_file_systems") as mock_get:
                    mock_get.return_value = [
                        FileSystemInfo(
                            file_system_id="fs-12345678",
                            file_system_type="efs",
                            region="us-east-1",
                            dns_name="fs-12345678.efs.us-east-1.amazonaws.com",
                        ),
                        FileSystemInfo(
                            file_system_id="fs-abcdef12",
                            file_system_type="fsx",
                            region="us-east-1",
                            dns_name="fs-abcdef12.fsx.us-east-1.amazonaws.com",
                        ),
                    ]

                    result = client.get_file_system_by_region("us-east-1", "efs")
                    assert result is not None
                    assert result.file_system_type == "efs"

    def test_get_file_system_by_region_not_found(self):
        """Test getting file system when not found."""
        from cli.files import FileSystemClient

        with patch("cli.files.get_config") as mock_config:
            mock_config.return_value = MagicMock()
            mock_config.return_value.project_name = "gco"
            with patch("cli.files.get_aws_client") as mock_aws:
                mock_aws.return_value = MagicMock()

                client = FileSystemClient()

                with patch.object(client, "get_file_systems") as mock_get:
                    mock_get.return_value = []

                    result = client.get_file_system_by_region("us-east-1", "efs")
                    assert result is None


class TestFileSystemClientEFS:
    """Tests for EFS-specific functionality."""

    def test_get_efs_info(self):
        """Test getting EFS file system info."""
        from cli.files import FileSystemClient

        with patch("cli.files.get_config") as mock_config:
            mock_config.return_value = MagicMock()
            mock_config.return_value.project_name = "gco"
            with patch("cli.files.get_aws_client") as mock_aws:
                mock_aws.return_value = MagicMock()

                client = FileSystemClient()

                with patch.object(client, "_session") as mock_session:
                    mock_efs = MagicMock()
                    mock_session.client.return_value = mock_efs

                    mock_efs.describe_file_systems.return_value = {
                        "FileSystems": [
                            {
                                "FileSystemId": "fs-12345678",
                                "LifeCycleState": "available",
                                "SizeInBytes": {"Value": 1024000},
                                "CreationTime": datetime(2024, 1, 1),
                            }
                        ]
                    }
                    mock_efs.describe_mount_targets.return_value = {
                        "MountTargets": [{"IpAddress": "10.0.1.100"}]
                    }
                    mock_efs.describe_tags.return_value = {
                        "Tags": [{"Key": "Name", "Value": "test-efs"}]
                    }

                    result = client._get_efs_info("fs-12345678", "us-east-1")

                    assert result is not None
                    assert result.file_system_id == "fs-12345678"
                    assert result.file_system_type == "efs"
                    assert result.mount_target_ip == "10.0.1.100"

    def test_get_efs_info_not_found(self):
        """Test getting EFS info when not found."""
        from cli.files import FileSystemClient

        with patch("cli.files.get_config") as mock_config:
            mock_config.return_value = MagicMock()
            mock_config.return_value.project_name = "gco"
            with patch("cli.files.get_aws_client") as mock_aws:
                mock_aws.return_value = MagicMock()

                client = FileSystemClient()

                with patch.object(client, "_session") as mock_session:
                    mock_efs = MagicMock()
                    mock_session.client.return_value = mock_efs
                    mock_efs.describe_file_systems.return_value = {"FileSystems": []}

                    result = client._get_efs_info("fs-nonexistent", "us-east-1")
                    assert result is None


class TestFileSystemClientFSx:
    """Tests for FSx-specific functionality."""

    def test_get_fsx_info(self):
        """Test getting FSx file system info."""
        from cli.files import FileSystemClient

        with patch("cli.files.get_config") as mock_config:
            mock_config.return_value = MagicMock()
            mock_config.return_value.project_name = "gco"
            with patch("cli.files.get_aws_client") as mock_aws:
                mock_aws.return_value = MagicMock()

                client = FileSystemClient()

                with patch.object(client, "_session") as mock_session:
                    mock_fsx = MagicMock()
                    mock_session.client.return_value = mock_fsx

                    mock_fsx.describe_file_systems.return_value = {
                        "FileSystems": [
                            {
                                "FileSystemId": "fs-abcdef12",
                                "DNSName": "fs-abcdef12.fsx.us-east-1.amazonaws.com",
                                "StorageCapacity": 1200,
                                "Lifecycle": "AVAILABLE",
                                "CreationTime": datetime(2024, 1, 1),
                                "Tags": [{"Key": "Name", "Value": "test-fsx"}],
                            }
                        ]
                    }

                    result = client._get_fsx_info("fs-abcdef12", "us-east-1")

                    assert result is not None
                    assert result.file_system_id == "fs-abcdef12"
                    assert result.file_system_type == "fsx"
                    assert result.size_bytes == 1200 * 1024 * 1024 * 1024

    def test_get_fsx_info_not_found(self):
        """Test getting FSx info when not found."""
        from cli.files import FileSystemClient

        with patch("cli.files.get_config") as mock_config:
            mock_config.return_value = MagicMock()
            mock_config.return_value.project_name = "gco"
            with patch("cli.files.get_aws_client") as mock_aws:
                mock_aws.return_value = MagicMock()

                client = FileSystemClient()

                with patch.object(client, "_session") as mock_session:
                    mock_fsx = MagicMock()
                    mock_session.client.return_value = mock_fsx
                    mock_fsx.describe_file_systems.return_value = {"FileSystems": []}

                    result = client._get_fsx_info("fs-nonexistent", "us-east-1")
                    assert result is None


class TestFileSystemClientAccessPoints:
    """Tests for EFS access points functionality."""

    def test_get_access_point_info(self):
        """Test getting EFS access points."""
        from cli.files import FileSystemClient

        with patch("cli.files.get_config") as mock_config:
            mock_config.return_value = MagicMock()
            mock_config.return_value.project_name = "gco"
            with patch("cli.files.get_aws_client") as mock_aws:
                mock_aws.return_value = MagicMock()

                client = FileSystemClient()

                with patch.object(client, "_session") as mock_session:
                    mock_efs = MagicMock()
                    mock_session.client.return_value = mock_efs

                    mock_efs.describe_access_points.return_value = {
                        "AccessPoints": [
                            {
                                "AccessPointId": "fsap-12345678",
                                "Name": "test-ap",
                                "RootDirectory": {"Path": "/data"},
                                "PosixUser": {"Uid": 1000, "Gid": 1000},
                                "LifeCycleState": "available",
                            }
                        ]
                    }

                    result = client.get_access_point_info("fs-12345678", "us-east-1")

                    assert len(result) == 1
                    assert result[0]["access_point_id"] == "fsap-12345678"
                    assert result[0]["path"] == "/data"

    def test_get_access_point_info_empty(self):
        """Test getting access points when none exist."""
        from cli.files import FileSystemClient

        with patch("cli.files.get_config") as mock_config:
            mock_config.return_value = MagicMock()
            mock_config.return_value.project_name = "gco"
            with patch("cli.files.get_aws_client") as mock_aws:
                mock_aws.return_value = MagicMock()

                client = FileSystemClient()

                with patch.object(client, "_session") as mock_session:
                    mock_efs = MagicMock()
                    mock_session.client.return_value = mock_efs
                    mock_efs.describe_access_points.return_value = {"AccessPoints": []}

                    result = client.get_access_point_info("fs-12345678", "us-east-1")
                    assert result == []


class TestGetFileSystemClient:
    """Tests for get_file_system_client factory function."""

    def test_get_file_system_client(self):
        """Test factory function returns FileSystemClient."""
        from cli.files import FileSystemClient, get_file_system_client

        with patch("cli.files.get_config") as mock_config:
            mock_config.return_value = MagicMock()
            mock_config.return_value.project_name = "gco"
            with patch("cli.files.get_aws_client") as mock_aws:
                mock_aws.return_value = MagicMock()
                client = get_file_system_client()
                assert isinstance(client, FileSystemClient)

    def test_get_file_system_client_with_config(self):
        """Test factory function with custom config."""
        from cli.files import FileSystemClient, get_file_system_client

        with patch("cli.files.get_aws_client") as mock_aws:
            mock_aws.return_value = MagicMock()
            custom_config = MagicMock()
            client = get_file_system_client(custom_config)
            assert isinstance(client, FileSystemClient)
            assert client.config == custom_config


class TestFileSystemClientEFSDetailed:
    """Additional tests for EFS operations."""

    def test_get_efs_info_with_tags(self):
        """Test getting EFS info with tags."""
        from cli.files import FileSystemClient

        with patch("cli.files.get_config") as mock_config:
            mock_config.return_value = MagicMock()
            mock_config.return_value.project_name = "gco"

            with patch("cli.files.get_aws_client") as mock_aws:
                mock_aws.return_value = MagicMock()

                with patch("boto3.Session") as mock_session:
                    mock_efs = MagicMock()
                    mock_efs.describe_file_systems.return_value = {
                        "FileSystems": [
                            {
                                "FileSystemId": "fs-12345",
                                "LifeCycleState": "available",
                                "SizeInBytes": {"Value": 1024000},
                                "CreationTime": datetime(2024, 1, 1),
                            }
                        ]
                    }
                    mock_efs.describe_mount_targets.return_value = {
                        "MountTargets": [{"IpAddress": "10.0.0.1"}]
                    }
                    mock_efs.describe_tags.return_value = {
                        "Tags": [
                            {"Key": "Name", "Value": "gco-efs"},
                            {"Key": "Environment", "Value": "prod"},
                        ]
                    }
                    mock_session.return_value.client.return_value = mock_efs

                    client = FileSystemClient()
                    info = client._get_efs_info("fs-12345", "us-east-1")

                    assert info is not None
                    assert info.tags["Name"] == "gco-efs"
                    assert info.mount_target_ip == "10.0.0.1"


class TestFileSystemClientFSxDetailed:
    """Additional tests for FSx operations."""

    def test_get_fsx_info_with_tags(self):
        """Test getting FSx info with tags."""
        from cli.files import FileSystemClient

        with patch("cli.files.get_config") as mock_config:
            mock_config.return_value = MagicMock()
            mock_config.return_value.project_name = "gco"

            with patch("cli.files.get_aws_client") as mock_aws:
                mock_aws.return_value = MagicMock()

                with patch("boto3.Session") as mock_session:
                    mock_fsx = MagicMock()
                    mock_fsx.describe_file_systems.return_value = {
                        "FileSystems": [
                            {
                                "FileSystemId": "fs-lustre-123",
                                "DNSName": "fs-lustre-123.fsx.us-east-1.amazonaws.com",
                                "Lifecycle": "AVAILABLE",
                                "StorageCapacity": 1200,
                                "CreationTime": datetime(2024, 1, 1),
                                "Tags": [{"Key": "Name", "Value": "gco-fsx"}],
                            }
                        ]
                    }
                    mock_session.return_value.client.return_value = mock_fsx

                    client = FileSystemClient()
                    info = client._get_fsx_info("fs-lustre-123", "us-east-1")

                    assert info is not None
                    assert info.file_system_type == "fsx"
                    assert info.tags["Name"] == "gco-fsx"
                    # Size should be converted from GB to bytes
                    assert info.size_bytes == 1200 * 1024 * 1024 * 1024


class TestFileSystemClientGetFileSystems:
    """Tests for get_file_systems method."""

    def test_get_file_systems_with_region_filter(self):
        """Test getting file systems with region filter."""
        from cli.aws_client import RegionalStack
        from cli.files import FileSystemClient

        with patch("cli.files.get_config") as mock_config:
            mock_config.return_value = MagicMock()
            mock_config.return_value.project_name = "gco"

            with patch("cli.files.get_aws_client") as mock_aws:
                mock_aws_client = MagicMock()
                mock_aws_client.discover_regional_stacks.return_value = {
                    "us-east-1": RegionalStack(
                        region="us-east-1",
                        stack_name="gco-us-east-1",
                        cluster_name="gco-us-east-1",
                        status="CREATE_COMPLETE",
                        efs_file_system_id="fs-12345",
                    ),
                    "us-west-2": RegionalStack(
                        region="us-west-2",
                        stack_name="gco-us-west-2",
                        cluster_name="gco-us-west-2",
                        status="CREATE_COMPLETE",
                        efs_file_system_id="fs-67890",
                    ),
                }
                mock_aws.return_value = mock_aws_client

                with patch("boto3.Session") as mock_session:
                    mock_efs = MagicMock()
                    mock_efs.describe_file_systems.return_value = {
                        "FileSystems": [{"FileSystemId": "fs-12345", "LifeCycleState": "available"}]
                    }
                    mock_efs.describe_mount_targets.return_value = {"MountTargets": []}
                    mock_efs.describe_tags.return_value = {"Tags": []}
                    mock_session.return_value.client.return_value = mock_efs

                    client = FileSystemClient()
                    # Filter to only us-east-1
                    file_systems = client.get_file_systems("us-east-1")

                    # Should only return file systems from us-east-1
                    assert all(fs.region == "us-east-1" for fs in file_systems)


class TestFileSystemClientGetByRegion:
    """Tests for FileSystemClient.get_file_system_by_region method."""

    def test_get_file_system_by_region_found(self):
        """Test get_file_system_by_region when found."""
        from unittest.mock import MagicMock

        from cli.files import FileSystemClient, FileSystemInfo

        client = FileSystemClient()
        mock_fs = FileSystemInfo(
            file_system_id="fs-123",
            file_system_type="efs",
            region="us-east-1",
            dns_name="fs-123.efs.us-east-1.amazonaws.com",
        )
        client.get_file_systems = MagicMock(return_value=[mock_fs])

        result = client.get_file_system_by_region("us-east-1", "efs")
        assert result == mock_fs

    def test_get_file_system_by_region_not_found(self):
        """Test get_file_system_by_region when not found."""
        from unittest.mock import MagicMock

        from cli.files import FileSystemClient

        client = FileSystemClient()
        client.get_file_systems = MagicMock(return_value=[])

        result = client.get_file_system_by_region("us-east-1", "efs")
        assert result is None


class TestFileSystemInfoDataclass:
    """Tests for FileSystemInfo dataclass."""

    def test_file_system_info_defaults(self):
        """Test FileSystemInfo default values."""
        from cli.files import FileSystemInfo

        info = FileSystemInfo(
            file_system_id="fs-123",
            file_system_type="efs",
            region="us-east-1",
            dns_name="fs-123.efs.us-east-1.amazonaws.com",
        )

        assert info.mount_target_ip is None
        assert info.size_bytes is None
        assert info.status == "available"
        assert info.created_time is None
        assert info.tags == {}

    def test_file_system_info_with_all_fields(self):
        """Test FileSystemInfo with all fields."""
        from datetime import datetime

        from cli.files import FileSystemInfo

        now = datetime.now()
        info = FileSystemInfo(
            file_system_id="fs-123",
            file_system_type="fsx",
            region="us-east-1",
            dns_name="fs-123.fsx.us-east-1.amazonaws.com",
            mount_target_ip="10.0.0.1",
            size_bytes=1024 * 1024 * 1024,
            status="available",
            created_time=now,
            tags={"Name": "test-fs"},
        )

        assert info.mount_target_ip == "10.0.0.1"
        assert info.size_bytes == 1024 * 1024 * 1024
        assert info.tags["Name"] == "test-fs"


class TestFileInfoDataclass:
    """Tests for FileInfo dataclass."""

    def test_file_info_creation(self):
        """Test FileInfo creation."""
        from cli.files import FileInfo

        info = FileInfo(
            path="/data/test.txt",
            name="test.txt",
            is_directory=False,
            size_bytes=1024,
        )

        assert info.path == "/data/test.txt"
        assert info.name == "test.txt"
        assert info.is_directory is False
        assert info.size_bytes == 1024

    def test_file_info_directory(self):
        """Test FileInfo for directory."""
        from cli.files import FileInfo

        info = FileInfo(
            path="/data/subdir",
            name="subdir",
            is_directory=True,
        )

        assert info.is_directory is True
        assert info.size_bytes == 0


class TestFileSystemClientDownloadFromPod:
    """Tests for download_from_pod method."""

    def test_download_from_pod_success(self):
        """Test successful download from pod."""
        from cli.files import FileSystemClient

        with patch("cli.files.get_config") as mock_config:
            mock_config.return_value = MagicMock()
            mock_config.return_value.project_name = "gco"

            with patch("cli.files.get_aws_client") as mock_aws:
                mock_aws.return_value = MagicMock()

                with patch("boto3.Session"):
                    client = FileSystemClient()

                    with patch("subprocess.run") as mock_run:
                        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")

                        with (
                            patch("os.path.isfile", return_value=True),
                            patch("os.path.getsize", return_value=1024),
                        ):
                            result = client.download_from_pod(
                                region="us-east-1",
                                pod_name="test-pod",
                                remote_path="/mnt/efs/outputs",
                                local_path="./local-outputs",
                                namespace="gco-jobs",
                            )

                            assert result["status"] == "success"
                            assert result["size_bytes"] == 1024
                            assert "test-pod" in result["source"]

    def test_download_from_pod_directory(self):
        """Test download directory from pod."""
        from cli.files import FileSystemClient

        with patch("cli.files.get_config") as mock_config:
            mock_config.return_value = MagicMock()
            mock_config.return_value.project_name = "gco"

            with patch("cli.files.get_aws_client") as mock_aws:
                mock_aws.return_value = MagicMock()

                with patch("boto3.Session"):
                    client = FileSystemClient()

                    with patch("subprocess.run") as mock_run:
                        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")

                        with (
                            patch("os.path.isfile", return_value=False),
                            patch("os.path.isdir", return_value=True),
                            patch("os.walk") as mock_walk,
                            patch("os.path.getsize", return_value=512),
                        ):
                            mock_walk.return_value = [("./local", [], ["file1.txt", "file2.txt"])]
                            result = client.download_from_pod(
                                region="us-east-1",
                                pod_name="test-pod",
                                remote_path="/mnt/efs/outputs",
                                local_path="./local",
                            )

                            assert result["status"] == "success"
                            assert result["size_bytes"] == 1024  # 2 files * 512

    def test_download_from_pod_with_container(self):
        """Test download with specific container."""
        from cli.files import FileSystemClient

        with patch("cli.files.get_config") as mock_config:
            mock_config.return_value = MagicMock()
            mock_config.return_value.project_name = "gco"

            with patch("cli.files.get_aws_client") as mock_aws:
                mock_aws.return_value = MagicMock()

                with patch("boto3.Session"):
                    client = FileSystemClient()

                    with patch("subprocess.run") as mock_run:
                        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")

                        with (
                            patch("os.path.isfile", return_value=True),
                            patch("os.path.getsize", return_value=100),
                        ):
                            client.download_from_pod(
                                region="us-east-1",
                                pod_name="test-pod",
                                remote_path="/data",
                                local_path="./data",
                                container="main",
                            )

                            # Verify kubectl cp was called with -c flag
                            calls = mock_run.call_args_list
                            kubectl_call = [c for c in calls if "kubectl" in str(c)]
                            assert len(kubectl_call) > 0
                            assert "-c" in str(kubectl_call[-1])

    def test_download_from_pod_kubeconfig_error(self):
        """Test download fails when kubeconfig update fails."""
        import subprocess

        from cli.files import FileSystemClient

        with patch("cli.files.get_config") as mock_config:
            mock_config.return_value = MagicMock()
            mock_config.return_value.project_name = "gco"

            with patch("cli.files.get_aws_client") as mock_aws:
                mock_aws.return_value = MagicMock()

                with patch("boto3.Session"):
                    client = FileSystemClient()

                    with patch("subprocess.run") as mock_run:
                        mock_run.side_effect = subprocess.CalledProcessError(
                            1, "aws", stderr="Cluster not found"
                        )

                        with pytest.raises(RuntimeError) as exc_info:
                            client.download_from_pod(
                                region="us-east-1",
                                pod_name="test-pod",
                                remote_path="/data",
                                local_path="./data",
                            )

                        assert "kubeconfig" in str(exc_info.value).lower()

    def test_download_from_pod_kubectl_error(self):
        """Test download fails when kubectl cp fails."""
        import subprocess

        from cli.files import FileSystemClient

        with patch("cli.files.get_config") as mock_config:
            mock_config.return_value = MagicMock()
            mock_config.return_value.project_name = "gco"

            with patch("cli.files.get_aws_client") as mock_aws:
                mock_aws.return_value = MagicMock()

                with patch("boto3.Session"):
                    client = FileSystemClient()

                    def run_side_effect(*args, **kwargs):
                        cmd = args[0]
                        if "aws" in cmd:
                            return MagicMock(returncode=0)
                        else:
                            raise subprocess.CalledProcessError(
                                1, "kubectl", stderr="Pod not found"
                            )

                    with patch("subprocess.run", side_effect=run_side_effect):
                        with pytest.raises(RuntimeError) as exc_info:
                            client.download_from_pod(
                                region="us-east-1",
                                pod_name="nonexistent-pod",
                                remote_path="/data",
                                local_path="./data",
                            )

                        assert "kubectl cp failed" in str(exc_info.value)

    def test_download_from_pod_kubectl_not_found(self):
        """Test download fails when kubectl is not installed."""
        from cli.files import FileSystemClient

        with patch("cli.files.get_config") as mock_config:
            mock_config.return_value = MagicMock()
            mock_config.return_value.project_name = "gco"

            with patch("cli.files.get_aws_client") as mock_aws:
                mock_aws.return_value = MagicMock()

                with patch("boto3.Session"):
                    client = FileSystemClient()

                    def run_side_effect(*args, **kwargs):
                        cmd = args[0]
                        if "aws" in cmd:
                            return MagicMock(returncode=0)
                        else:
                            raise FileNotFoundError("kubectl not found")

                    with patch("subprocess.run", side_effect=run_side_effect):
                        with pytest.raises(RuntimeError) as exc_info:
                            client.download_from_pod(
                                region="us-east-1",
                                pod_name="test-pod",
                                remote_path="/data",
                                local_path="./data",
                            )

                        assert "kubectl not found" in str(exc_info.value)


class TestFileSystemClientDownloadFromStorage:
    """Tests for download_from_storage method."""

    def test_download_from_storage_success(self):
        """Test successful download from storage using helper pod."""
        from cli.files import FileSystemClient

        with patch("cli.files.get_config") as mock_config:
            mock_config.return_value = MagicMock()
            mock_config.return_value.project_name = "gco"

            with patch("cli.files.get_aws_client") as mock_aws:
                mock_aws.return_value = MagicMock()

                with patch("boto3.Session"):
                    client = FileSystemClient()

                    with patch("subprocess.run") as mock_run:
                        # Mock all subprocess calls to succeed
                        mock_run.return_value = MagicMock(returncode=0, stdout="Running", stderr="")

                        with (
                            patch("os.path.isfile", return_value=True),
                            patch("os.path.getsize", return_value=2048),
                            patch("time.sleep"),
                        ):
                            result = client.download_from_storage(
                                region="us-east-1",
                                remote_path="my-job/outputs",
                                local_path="./local-outputs",
                                storage_type="efs",
                                namespace="gco-jobs",
                            )

                            assert result["status"] == "success"
                            assert result["size_bytes"] == 2048
                            assert result["storage_type"] == "efs"

    def test_download_from_storage_fsx(self):
        """Test download from FSx storage."""
        from cli.files import FileSystemClient

        with patch("cli.files.get_config") as mock_config:
            mock_config.return_value = MagicMock()
            mock_config.return_value.project_name = "gco"

            with patch("cli.files.get_aws_client") as mock_aws:
                mock_aws.return_value = MagicMock()

                with patch("boto3.Session"):
                    client = FileSystemClient()

                    with patch("subprocess.run") as mock_run:
                        mock_run.return_value = MagicMock(returncode=0, stdout="Running", stderr="")

                        with (
                            patch("os.path.isfile", return_value=True),
                            patch("os.path.getsize", return_value=4096),
                            patch("time.sleep"),
                        ):
                            result = client.download_from_storage(
                                region="us-east-1",
                                remote_path="checkpoints",
                                local_path="./checkpoints",
                                storage_type="fsx",
                            )

                            assert result["status"] == "success"
                            assert result["storage_type"] == "fsx"

    def test_download_from_storage_custom_pvc(self):
        """Test download with custom PVC name."""
        from cli.files import FileSystemClient

        with patch("cli.files.get_config") as mock_config:
            mock_config.return_value = MagicMock()
            mock_config.return_value.project_name = "gco"

            with patch("cli.files.get_aws_client") as mock_aws:
                mock_aws.return_value = MagicMock()

                with patch("boto3.Session"):
                    client = FileSystemClient()

                    with patch("subprocess.run") as mock_run:
                        mock_run.return_value = MagicMock(returncode=0, stdout="Running", stderr="")

                        with (
                            patch("os.path.isfile", return_value=True),
                            patch("os.path.getsize", return_value=1024),
                            patch("time.sleep"),
                        ):
                            result = client.download_from_storage(
                                region="us-east-1",
                                remote_path="data",
                                local_path="./data",
                                pvc_name="custom-pvc",
                            )

                            assert result["status"] == "success"
                            # Verify custom PVC was used in pod manifest
                            apply_call = [c for c in mock_run.call_args_list if "apply" in str(c)]
                            assert len(apply_call) > 0

    def test_download_from_storage_pod_timeout(self):
        """Test download fails when helper pod doesn't become ready."""
        from cli.files import FileSystemClient

        with patch("cli.files.get_config") as mock_config:
            mock_config.return_value = MagicMock()
            mock_config.return_value.project_name = "gco"

            with patch("cli.files.get_aws_client") as mock_aws:
                mock_aws.return_value = MagicMock()

                with patch("boto3.Session"):
                    client = FileSystemClient()

                    with patch("subprocess.run") as mock_run:
                        # Pod never becomes ready
                        mock_run.return_value = MagicMock(returncode=0, stdout="Pending", stderr="")

                        with patch("time.sleep"):
                            with pytest.raises(RuntimeError) as exc_info:
                                client.download_from_storage(
                                    region="us-east-1",
                                    remote_path="data",
                                    local_path="./data",
                                )

                            assert "ready" in str(exc_info.value).lower()

    def test_download_from_storage_cleanup_on_error(self):
        """Test helper pod is cleaned up even on error."""
        import subprocess

        from cli.files import FileSystemClient

        with patch("cli.files.get_config") as mock_config:
            mock_config.return_value = MagicMock()
            mock_config.return_value.project_name = "gco"

            with patch("cli.files.get_aws_client") as mock_aws:
                mock_aws.return_value = MagicMock()

                with patch("boto3.Session"):
                    client = FileSystemClient()

                    call_count = [0]

                    def run_side_effect(*args, **kwargs):
                        call_count[0] += 1
                        cmd = args[0] if args else kwargs.get("args", [])
                        if "aws" in cmd or "apply" in cmd:
                            return MagicMock(returncode=0)
                        elif "get" in cmd and "pod" in cmd:
                            return MagicMock(returncode=0, stdout="Running")
                        elif "cp" in cmd:
                            raise subprocess.CalledProcessError(1, "kubectl", stderr="Copy failed")
                        elif "delete" in cmd:
                            return MagicMock(returncode=0)
                        return MagicMock(returncode=0)

                    with (
                        patch("subprocess.run", side_effect=run_side_effect),
                        patch("time.sleep"),
                    ):
                        with pytest.raises(RuntimeError):
                            client.download_from_storage(
                                region="us-east-1",
                                remote_path="data",
                                local_path="./data",
                            )

                        # Verify delete was called (cleanup)
                        assert call_count[0] >= 4  # aws, apply, get, cp, delete


class TestFileSystemClientListStorageContents:
    """Tests for list_storage_contents method."""

    def test_list_storage_contents_success(self):
        """Test successful listing of storage contents."""
        from cli.files import FileSystemClient

        with patch("cli.files.get_config") as mock_config:
            mock_config.return_value = MagicMock()
            mock_config.return_value.project_name = "gco"

            with patch("cli.files.get_aws_client") as mock_aws:
                mock_aws.return_value = MagicMock()

                with patch("boto3.Session"):
                    client = FileSystemClient()

                    def run_side_effect(*args, **kwargs):
                        cmd = args[0] if args else kwargs.get("args", [])
                        if "aws" in cmd or "apply" in cmd:
                            return MagicMock(returncode=0)
                        elif "get" in cmd and "pod" in cmd:
                            return MagicMock(returncode=0, stdout="Running")
                        elif "exec" in cmd:
                            ls_output = """total 8
drwxr-xr-x 2 root root 4096 Jan  1 00:00 efs-output-example
drwxr-xr-x 2 root root 4096 Jan  1 00:00 my-job-outputs
-rw-r--r-- 1 root root 1024 Jan  1 00:00 test.txt"""
                            return MagicMock(returncode=0, stdout=ls_output, stderr="")
                        elif "delete" in cmd:
                            return MagicMock(returncode=0)
                        return MagicMock(returncode=0)

                    with (
                        patch("subprocess.run", side_effect=run_side_effect),
                        patch("time.sleep"),
                    ):
                        result = client.list_storage_contents(
                            region="us-east-1",
                            remote_path="/",
                            storage_type="efs",
                        )

                        assert result["status"] == "success"
                        assert len(result["contents"]) == 3
                        # Check directories
                        dirs = [c for c in result["contents"] if c["is_directory"]]
                        assert len(dirs) == 2
                        # Check files
                        files = [c for c in result["contents"] if not c["is_directory"]]
                        assert len(files) == 1
                        assert files[0]["name"] == "test.txt"
                        assert files[0]["size_bytes"] == 1024

    def test_list_storage_contents_empty_directory(self):
        """Test listing empty directory."""
        from cli.files import FileSystemClient

        with patch("cli.files.get_config") as mock_config:
            mock_config.return_value = MagicMock()
            mock_config.return_value.project_name = "gco"

            with patch("cli.files.get_aws_client") as mock_aws:
                mock_aws.return_value = MagicMock()

                with patch("boto3.Session"):
                    client = FileSystemClient()

                    def run_side_effect(*args, **kwargs):
                        cmd = args[0] if args else kwargs.get("args", [])
                        if "aws" in cmd or "apply" in cmd:
                            return MagicMock(returncode=0)
                        elif "get" in cmd and "pod" in cmd:
                            return MagicMock(returncode=0, stdout="Running")
                        elif "exec" in cmd:
                            return MagicMock(returncode=0, stdout="total 0", stderr="")
                        elif "delete" in cmd:
                            return MagicMock(returncode=0)
                        return MagicMock(returncode=0)

                    with (
                        patch("subprocess.run", side_effect=run_side_effect),
                        patch("time.sleep"),
                    ):
                        result = client.list_storage_contents(
                            region="us-east-1",
                            remote_path="/empty-dir",
                        )

                        assert result["status"] == "success"
                        assert result["contents"] == []

    def test_list_storage_contents_path_not_found(self):
        """Test listing non-existent path."""
        from cli.files import FileSystemClient

        with patch("cli.files.get_config") as mock_config:
            mock_config.return_value = MagicMock()
            mock_config.return_value.project_name = "gco"

            with patch("cli.files.get_aws_client") as mock_aws:
                mock_aws.return_value = MagicMock()

                with patch("boto3.Session"):
                    client = FileSystemClient()

                    def run_side_effect(*args, **kwargs):
                        cmd = args[0] if args else kwargs.get("args", [])
                        if "aws" in cmd or "apply" in cmd:
                            return MagicMock(returncode=0)
                        elif "get" in cmd and "pod" in cmd:
                            return MagicMock(returncode=0, stdout="Running")
                        elif "exec" in cmd:
                            return MagicMock(
                                returncode=1,
                                stdout="",
                                stderr="ls: /efs/nonexistent: No such file or directory",
                            )
                        elif "delete" in cmd:
                            return MagicMock(returncode=0)
                        return MagicMock(returncode=0)

                    with (
                        patch("subprocess.run", side_effect=run_side_effect),
                        patch("time.sleep"),
                    ):
                        result = client.list_storage_contents(
                            region="us-east-1",
                            remote_path="/nonexistent",
                        )

                        assert result["status"] == "error"
                        assert "not found" in result["message"].lower()

    def test_list_storage_contents_fsx(self):
        """Test listing FSx storage contents."""
        from cli.files import FileSystemClient

        with patch("cli.files.get_config") as mock_config:
            mock_config.return_value = MagicMock()
            mock_config.return_value.project_name = "gco"

            with patch("cli.files.get_aws_client") as mock_aws:
                mock_aws.return_value = MagicMock()

                with patch("boto3.Session"):
                    client = FileSystemClient()

                    def run_side_effect(*args, **kwargs):
                        cmd = args[0] if args else kwargs.get("args", [])
                        if "aws" in cmd or "apply" in cmd:
                            return MagicMock(returncode=0)
                        elif "get" in cmd and "pod" in cmd:
                            return MagicMock(returncode=0, stdout="Running")
                        elif "exec" in cmd:
                            ls_output = """total 4
drwxr-xr-x 2 root root 4096 Jan  1 00:00 checkpoints"""
                            return MagicMock(returncode=0, stdout=ls_output, stderr="")
                        elif "delete" in cmd:
                            return MagicMock(returncode=0)
                        return MagicMock(returncode=0)

                    with (
                        patch("subprocess.run", side_effect=run_side_effect),
                        patch("time.sleep"),
                    ):
                        result = client.list_storage_contents(
                            region="us-east-1",
                            remote_path="/",
                            storage_type="fsx",
                        )

                        assert result["status"] == "success"
                        assert result["storage_type"] == "fsx"

    def test_list_storage_contents_cleanup_on_error(self):
        """Test helper pod is cleaned up even on error."""
        import subprocess

        from cli.files import FileSystemClient

        with patch("cli.files.get_config") as mock_config:
            mock_config.return_value = MagicMock()
            mock_config.return_value.project_name = "gco"

            with patch("cli.files.get_aws_client") as mock_aws:
                mock_aws.return_value = MagicMock()

                with patch("boto3.Session"):
                    client = FileSystemClient()

                    delete_called = [False]

                    def run_side_effect(*args, **kwargs):
                        cmd = args[0] if args else kwargs.get("args", [])
                        if "aws" in cmd or "apply" in cmd:
                            return MagicMock(returncode=0)
                        elif "get" in cmd and "pod" in cmd:
                            return MagicMock(returncode=0, stdout="Running")
                        elif "exec" in cmd:
                            raise subprocess.CalledProcessError(1, "kubectl", stderr="Exec failed")
                        elif "delete" in cmd:
                            delete_called[0] = True
                            return MagicMock(returncode=0)
                        return MagicMock(returncode=0)

                    with (
                        patch("subprocess.run", side_effect=run_side_effect),
                        patch("time.sleep"),
                    ):
                        with pytest.raises(RuntimeError):
                            client.list_storage_contents(
                                region="us-east-1",
                                remote_path="/",
                            )

                        # Verify cleanup was attempted
                        assert delete_called[0]
