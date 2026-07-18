"""
Extended coverage for cli/files.FileSystemClient.

Exercises get_file_systems against RegionalStacks that expose both EFS
and FSx file system IDs, plus the error handling in _get_efs_info and
_get_fsx_info when the AWS APIs raise ClientError. Pairs with
test_files.py which covers the dataclass layer.
"""

from unittest.mock import MagicMock, patch

import pytest
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


class TestFileSystemClientDataSync:
    """Tests for DataSync operations."""

    @pytest.mark.parametrize(
        ("region", "partition"),
        (("cn-north-1", "aws-cn"), ("us-gov-west-1", "aws-us-gov")),
    )
    def test_datasync_arns_use_target_partition(self, region, partition):
        """EFS and S3 DataSync ARNs follow China and GovCloud metadata."""
        from cli.files import FileSystemClient, FileSystemInfo

        with (
            patch("cli.files.get_config", return_value=MagicMock()),
            patch("cli.files.get_aws_client", return_value=MagicMock()),
        ):
            client = FileSystemClient()

        file_system_id = "fs-efs123"
        file_system = FileSystemInfo(
            file_system_id=file_system_id,
            file_system_type="efs",
            region=region,
            dns_name="unused",
        )
        datasync = MagicMock()
        datasync.create_location_efs.return_value = {"LocationArn": "source"}
        datasync.create_location_s3.return_value = {"LocationArn": "destination"}
        datasync.create_task.return_value = {"TaskArn": "task"}
        client._session = MagicMock()
        client._session.client.return_value = datasync

        with (
            patch.object(client, "get_file_systems", return_value=[file_system]),
            patch.object(client, "_get_account_id", return_value="123456789012"),
            patch.object(
                client,
                "_get_subnet_arn",
                return_value=f"arn:{partition}:ec2:{region}:123456789012:subnet/subnet-1",
            ),
            patch.object(
                client,
                "_get_security_group_arn",
                return_value=(f"arn:{partition}:ec2:{region}:123456789012:security-group/sg-1"),
            ),
            patch.object(
                client,
                "_get_datasync_role_arn",
                return_value=f"arn:{partition}:iam::123456789012:role/datasync",
            ),
        ):
            assert (
                client.create_datasync_download_task(
                    file_system_id=file_system_id,
                    region=region,
                    source_path="/data",
                    destination_bucket="gco-downloads",
                )
                == "task"
            )

        assert datasync.create_location_efs.call_args.kwargs["EfsFilesystemArn"] == (
            f"arn:{partition}:elasticfilesystem:{region}:123456789012:file-system/{file_system_id}"
        )
        assert datasync.create_location_s3.call_args.kwargs["S3BucketArn"] == (
            f"arn:{partition}:s3:::gco-downloads"
        )

    def test_create_datasync_download_task_efs(self):
        """Test creating DataSync task for EFS."""
        from cli.files import FileSystemClient, FileSystemInfo

        with patch("cli.files.get_config") as mock_config:
            mock_config.return_value = MagicMock()

            with patch("cli.files.get_aws_client") as mock_aws:
                mock_aws.return_value = MagicMock()

                client = FileSystemClient()

                # Mock file system info
                efs_info = FileSystemInfo(
                    file_system_id="fs-efs123",
                    file_system_type="efs",
                    region="us-east-1",
                    dns_name="fs-efs123.efs.us-east-1.amazonaws.com",
                )

                with (
                    patch.object(client, "get_file_systems", return_value=[efs_info]),
                    patch.object(client, "_session") as mock_session,
                ):
                    mock_datasync = MagicMock()
                    mock_datasync.create_location_efs.return_value = {
                        "LocationArn": "arn:aws:datasync:us-east-1:123:location/loc-efs"
                    }
                    mock_datasync.create_location_s3.return_value = {
                        "LocationArn": "arn:aws:datasync:us-east-1:123:location/loc-s3"
                    }
                    mock_datasync.create_task.return_value = {
                        "TaskArn": "arn:aws:datasync:us-east-1:123:task/task-123"
                    }
                    mock_session.client.return_value = mock_datasync

                    with (
                        patch.object(client, "_get_account_id", return_value="123456789012"),
                        patch.object(
                            client,
                            "_get_subnet_arn",
                            return_value="arn:aws:ec2:us-east-1:123:subnet/subnet-123",
                        ),
                        patch.object(
                            client,
                            "_get_security_group_arn",
                            return_value="arn:aws:ec2:us-east-1:123:security-group/sg-123",
                        ),
                        patch.object(
                            client,
                            "_get_datasync_role_arn",
                            return_value="arn:aws:iam::123:role/datasync-role",
                        ),
                    ):
                        task_arn = client.create_datasync_download_task(
                            file_system_id="fs-efs123",
                            region="us-east-1",
                            source_path="/data",
                            destination_bucket="my-bucket",
                            destination_prefix="downloads/",
                        )

                        assert "task-123" in task_arn

    def test_create_datasync_download_task_fsx(self):
        """Test creating DataSync task for FSx."""
        from cli.files import FileSystemClient, FileSystemInfo

        with patch("cli.files.get_config") as mock_config:
            mock_config.return_value = MagicMock()

            with patch("cli.files.get_aws_client") as mock_aws:
                mock_aws.return_value = MagicMock()

                client = FileSystemClient()

                # Mock file system info
                fsx_info = FileSystemInfo(
                    file_system_id="fs-fsx456",
                    file_system_type="fsx",
                    region="us-east-1",
                    dns_name="fs-fsx456.fsx.us-east-1.amazonaws.com",
                )

                with (
                    patch.object(client, "get_file_systems", return_value=[fsx_info]),
                    patch.object(client, "_session") as mock_session,
                ):
                    mock_datasync = MagicMock()
                    mock_datasync.create_location_fsx_lustre.return_value = {
                        "LocationArn": "arn:aws:datasync:us-east-1:123:location/loc-fsx"
                    }
                    mock_datasync.create_location_s3.return_value = {
                        "LocationArn": "arn:aws:datasync:us-east-1:123:location/loc-s3"
                    }
                    mock_datasync.create_task.return_value = {
                        "TaskArn": "arn:aws:datasync:us-east-1:123:task/task-456"
                    }
                    mock_session.client.return_value = mock_datasync

                    with (
                        patch.object(client, "_get_account_id", return_value="123456789012"),
                        patch.object(
                            client,
                            "_get_security_group_arn",
                            return_value="arn:aws:ec2:us-east-1:123:security-group/sg-123",
                        ),
                        patch.object(
                            client,
                            "_get_datasync_role_arn",
                            return_value="arn:aws:iam::123:role/datasync-role",
                        ),
                    ):
                        task_arn = client.create_datasync_download_task(
                            file_system_id="fs-fsx456",
                            region="us-east-1",
                            source_path="/data",
                            destination_bucket="my-bucket",
                        )

                        assert "task-456" in task_arn

    def test_create_datasync_download_task_fs_not_found(self):
        """Test creating DataSync task when file system not found."""
        from cli.files import FileSystemClient

        with patch("cli.files.get_config") as mock_config:
            mock_config.return_value = MagicMock()

            with patch("cli.files.get_aws_client") as mock_aws:
                mock_aws.return_value = MagicMock()

                client = FileSystemClient()

                with (
                    patch.object(client, "get_file_systems", return_value=[]),
                    pytest.raises(ValueError, match="not found"),
                ):
                    client.create_datasync_download_task(
                        file_system_id="fs-nonexistent",
                        region="us-east-1",
                        source_path="/data",
                        destination_bucket="my-bucket",
                    )


class TestFileSystemClientHelperMethods:
    """Tests for helper methods."""

    def test_get_account_id(self):
        """Test getting AWS account ID."""
        from cli.files import FileSystemClient

        with patch("cli.files.get_config") as mock_config:
            mock_config.return_value = MagicMock()

            with patch("cli.files.get_aws_client") as mock_aws:
                mock_aws.return_value = MagicMock()

                client = FileSystemClient()

                with patch.object(client, "_session") as mock_session:
                    mock_sts = MagicMock()
                    mock_sts.get_caller_identity.return_value = {"Account": "123456789012"}
                    mock_session.client.return_value = mock_sts

                    account_id = client._get_account_id()
                    assert account_id == "123456789012"

    def test_get_subnet_arn_not_implemented(self):
        """Test that _get_subnet_arn raises NotImplementedError."""
        from cli.files import FileSystemClient

        with patch("cli.files.get_config") as mock_config:
            mock_config.return_value = MagicMock()

            with patch("cli.files.get_aws_client") as mock_aws:
                mock_aws.return_value = MagicMock()

                client = FileSystemClient()

                with pytest.raises(NotImplementedError):
                    client._get_subnet_arn("us-east-1")

    def test_get_security_group_arn_not_implemented(self):
        """Test that _get_security_group_arn raises NotImplementedError."""
        from cli.files import FileSystemClient

        with patch("cli.files.get_config") as mock_config:
            mock_config.return_value = MagicMock()

            with patch("cli.files.get_aws_client") as mock_aws:
                mock_aws.return_value = MagicMock()

                client = FileSystemClient()

                with pytest.raises(NotImplementedError):
                    client._get_security_group_arn("us-east-1")

    def test_get_datasync_role_arn_not_implemented(self):
        """Test that _get_datasync_role_arn raises NotImplementedError."""
        from cli.files import FileSystemClient

        with patch("cli.files.get_config") as mock_config:
            mock_config.return_value = MagicMock()

            with patch("cli.files.get_aws_client") as mock_aws:
                mock_aws.return_value = MagicMock()

                client = FileSystemClient()

                with pytest.raises(NotImplementedError):
                    client._get_datasync_role_arn("us-east-1")


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
