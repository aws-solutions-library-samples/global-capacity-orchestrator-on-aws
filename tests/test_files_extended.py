"""
Extended coverage for cli/files.FileSystemClient.

Exercises get_file_systems against RegionalStacks that expose both EFS
and FSx file system IDs, plus the error handling in _get_efs_info and
_get_fsx_info when the AWS APIs raise ClientError. Pairs with
test_files.py which covers the dataclass layer.
"""

from unittest.mock import MagicMock, patch

from botocore.exceptions import ClientError


class TestFileSystemClientGetFileSystems:
    """Tests for FileSystemClient.get_file_systems with EFS and FSx."""

    def test_get_file_systems_with_efs_and_fsx(self):
        """Test getting file systems with both EFS and FSx."""
        from cli.aws_client import RegionalStack
        from cli.files import FileSystemClient, FileSystemInfo

        with patch("cli.files.get_config") as mock_config:
            mock_config.return_value = MagicMock()

            with patch("cli.files.get_aws_client") as mock_aws:
                mock_aws_client = MagicMock()
                mock_aws_client.discover_regional_stacks.return_value = {
                    "us-east-1": RegionalStack(
                        region="us-east-1",
                        stack_name="gco-us-east-1",
                        cluster_name="gco-us-east-1",
                        status="CREATE_COMPLETE",
                        efs_file_system_id="fs-efs123",
                        fsx_file_system_id="fs-fsx456",
                    ),
                }
                mock_aws.return_value = mock_aws_client

                client = FileSystemClient()

                # Mock EFS info
                efs_info = FileSystemInfo(
                    file_system_id="fs-efs123",
                    file_system_type="efs",
                    region="us-east-1",
                    dns_name="fs-efs123.efs.us-east-1.amazonaws.com",
                )

                # Mock FSx info
                fsx_info = FileSystemInfo(
                    file_system_id="fs-fsx456",
                    file_system_type="fsx",
                    region="us-east-1",
                    dns_name="fs-fsx456.fsx.us-east-1.amazonaws.com",
                )

                with (
                    patch.object(client, "_get_efs_info", return_value=efs_info),
                    patch.object(client, "_get_fsx_info", return_value=fsx_info),
                ):
                    file_systems = client.get_file_systems()

                    assert len(file_systems) == 2
                    assert any(fs.file_system_type == "efs" for fs in file_systems)
                    assert any(fs.file_system_type == "fsx" for fs in file_systems)


class TestFileSystemClientEFSErrors:
    """Tests for EFS error handling."""

    def test_get_efs_info_client_error(self):
        """Test getting EFS info with ClientError."""
        from cli.files import FileSystemClient

        with patch("cli.files.get_config") as mock_config:
            mock_config.return_value = MagicMock()

            with patch("cli.files.get_aws_client") as mock_aws:
                mock_aws.return_value = MagicMock()

                client = FileSystemClient()

                with patch.object(client, "_session") as mock_session:
                    mock_efs = MagicMock()
                    mock_efs.describe_file_systems.side_effect = ClientError(
                        {"Error": {"Code": "FileSystemNotFound", "Message": "Not found"}},
                        "DescribeFileSystems",
                    )
                    mock_session.client.return_value = mock_efs

                    result = client._get_efs_info("fs-invalid", "us-east-1")
                    assert result is None


class TestFileSystemClientFSxErrors:
    """Tests for FSx error handling."""

    def test_get_fsx_info_client_error(self):
        """Test getting FSx info with ClientError."""
        from cli.files import FileSystemClient

        with patch("cli.files.get_config") as mock_config:
            mock_config.return_value = MagicMock()

            with patch("cli.files.get_aws_client") as mock_aws:
                mock_aws.return_value = MagicMock()

                client = FileSystemClient()

                with patch.object(client, "_session") as mock_session:
                    mock_fsx = MagicMock()
                    mock_fsx.describe_file_systems.side_effect = ClientError(
                        {"Error": {"Code": "FileSystemNotFound", "Message": "Not found"}},
                        "DescribeFileSystems",
                    )
                    mock_session.client.return_value = mock_fsx

                    result = client._get_fsx_info("fs-invalid", "us-east-1")
                    assert result is None


class TestFileSystemClientEFSMountTargets:
    """Tests for EFS mount target handling."""

    def test_get_efs_info_uses_partition_url_suffix(self):
        """China EFS DNS names use botocore's partition URL suffix."""
        from cli.files import FileSystemClient

        with (
            patch("cli.files.get_config", return_value=MagicMock()),
            patch("cli.files.get_aws_client", return_value=MagicMock()),
        ):
            client = FileSystemClient()

        efs = MagicMock()
        efs.describe_file_systems.return_value = {
            "FileSystems": [
                {
                    "FileSystemId": "fs-12345",
                    "LifeCycleState": "available",
                    "SizeInBytes": {"Value": 1024},
                }
            ]
        }
        efs.describe_mount_targets.return_value = {"MountTargets": []}
        efs.describe_tags.return_value = {"Tags": []}
        client._session = MagicMock()
        client._session.client.return_value = efs

        result = client._get_efs_info("fs-12345", "cn-north-1")

        assert result is not None
        assert result.dns_name == "fs-12345.efs.cn-north-1.amazonaws.com.cn"

    def test_get_efs_info_no_mount_targets(self):
        """Test getting EFS info when no mount targets exist."""
        from cli.files import FileSystemClient

        with patch("cli.files.get_config") as mock_config:
            mock_config.return_value = MagicMock()

            with patch("cli.files.get_aws_client") as mock_aws:
                mock_aws.return_value = MagicMock()

                client = FileSystemClient()

                with patch.object(client, "_session") as mock_session:
                    mock_efs = MagicMock()
                    mock_efs.describe_file_systems.return_value = {
                        "FileSystems": [
                            {
                                "FileSystemId": "fs-12345",
                                "LifeCycleState": "available",
                                "SizeInBytes": {"Value": 1024000},
                            }
                        ]
                    }
                    mock_efs.describe_mount_targets.return_value = {"MountTargets": []}
                    mock_efs.describe_tags.return_value = {"Tags": []}
                    mock_session.client.return_value = mock_efs

                    result = client._get_efs_info("fs-12345", "us-east-1")

                    assert result is not None
                    assert result.mount_target_ip is None


class TestFileSystemClientFSxDetails:
    """Tests for FSx detailed info."""

    def test_get_fsx_info_no_dns_name(self):
        """Test getting FSx info when DNS name is missing."""
        from cli.files import FileSystemClient

        with patch("cli.files.get_config") as mock_config:
            mock_config.return_value = MagicMock()

            with patch("cli.files.get_aws_client") as mock_aws:
                mock_aws.return_value = MagicMock()

                client = FileSystemClient()

                with patch.object(client, "_session") as mock_session:
                    mock_fsx = MagicMock()
                    mock_fsx.describe_file_systems.return_value = {
                        "FileSystems": [
                            {
                                "FileSystemId": "fs-fsx123",
                                "Lifecycle": "AVAILABLE",
                                "StorageCapacity": 1200,
                                "Tags": [],
                            }
                        ]
                    }
                    mock_session.client.return_value = mock_fsx

                    result = client._get_fsx_info("fs-fsx123", "us-east-1")

                    assert result is not None
                    assert result.dns_name == ""
