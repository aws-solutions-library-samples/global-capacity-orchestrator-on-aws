"""File system commands."""

import sys
from typing import Any

import click

from ..config import GCOConfig
from ..files import get_file_system_client
from ..output import format_file_system_table, get_output_formatter

pass_config = click.make_pass_decorator(GCOConfig, ensure=True)


@click.group()
@pass_config
def files(config: Any) -> None:
    """Manage file systems (EFS/FSx)."""


@files.command("list")
@click.option("--region", "-r", help="Filter by region")
@pass_config
def list_file_systems(config: Any, region: Any) -> None:
    """List file systems across GCO stacks."""
    formatter = get_output_formatter(config)
    fs_client = get_file_system_client(config)

    try:
        file_systems = fs_client.get_file_systems(region)

        if not file_systems:
            formatter.print_warning("No file systems found")
            return

        if config.output_format == "table":
            print(format_file_system_table(file_systems))
        else:
            formatter.print(file_systems)

    except Exception as e:
        formatter.print_error(f"Failed to list file systems: {e}")
        sys.exit(1)


@files.command("get")
@click.argument("region")
@click.option(
    "--type",
    "-t",
    "fs_type",
    type=click.Choice(["efs", "fsx"]),
    default="efs",
    help="File system type",
)
@pass_config
def get_file_system(config: Any, region: Any, fs_type: Any) -> None:
    """Get file system details for a region."""
    formatter = get_output_formatter(config)
    fs_client = get_file_system_client(config)

    try:
        fs = fs_client.get_file_system_by_region(region, fs_type)
        if fs:
            formatter.print(fs)
        else:
            formatter.print_error(f"No {fs_type.upper()} file system found in {region}")
            sys.exit(1)
    except Exception as e:
        formatter.print_error(f"Failed to get file system: {e}")
        sys.exit(1)


@files.command("access-points")
@click.argument("file_system_id")
@click.option("--region", "-r", required=True, help="AWS region")
@pass_config
def list_access_points(config: Any, file_system_id: Any, region: Any) -> None:
    """List EFS access points for a file system."""
    formatter = get_output_formatter(config)
    fs_client = get_file_system_client(config)

    try:
        access_points = fs_client.get_access_point_info(file_system_id, region)

        if not access_points:
            formatter.print_warning("No access points found")
            return

        formatter.print(access_points)

    except Exception as e:
        formatter.print_error(f"Failed to list access points: {e}")
        sys.exit(1)


@files.command("ls")
@click.argument("remote_path", default="/")
@click.option("--region", "-r", required=True, help="AWS region")
@click.option("--namespace", "-n", default="gco-jobs", help="Kubernetes namespace")
@click.option(
    "--storage-type",
    "-t",
    type=click.Choice(["efs", "fsx"]),
    default="efs",
    help="Storage type (default: efs)",
)
@click.option("--pvc", help="PVC name (default: gco-shared-storage for EFS)")
@pass_config
def list_storage_contents(
    config: Any, remote_path: Any, region: Any, namespace: Any, storage_type: Any, pvc: Any
) -> None:
    """List contents of EFS/FSx storage.

    Creates a temporary helper pod to mount the storage and list contents,
    then cleans up automatically. Useful for discovering job output directories.

    REMOTE_PATH is relative to the storage root (default: / for root listing)

    REQUIREMENTS:
    - kubectl installed and in PATH
    - EKS access entry configured for your IAM principal
    - AWS credentials with eks:DescribeCluster permission

    Examples:
        gco files ls -r us-east-1                    # List root of EFS
        gco files ls efs-output-example -r us-east-1 # List job output directory
        gco files ls -r us-west-2 -t fsx             # List FSx root
    """
    formatter = get_output_formatter(config)
    fs_client = get_file_system_client(config)

    try:
        formatter.print_info(f"Listing {remote_path} on {storage_type.upper()}...")

        result = fs_client.list_storage_contents(
            region=region,
            remote_path=remote_path,
            storage_type=storage_type,
            namespace=namespace,
            pvc_name=pvc,
        )

        if result["status"] == "success":
            formatter.print_success(result["message"])
            if result["contents"]:
                # Format as table
                print("\n  TYPE  SIZE       NAME")
                print("  " + "-" * 40)
                for item in result["contents"]:
                    item_type = "DIR " if item["is_directory"] else "FILE"
                    size = f"{item['size_bytes']:>10}" if not item["is_directory"] else "         -"
                    print(f"  {item_type} {size}  {item['name']}")
            else:
                formatter.print_warning("Directory is empty")
        else:
            formatter.print_error(result["message"])
            sys.exit(1)

    except Exception as e:
        formatter.print_error(f"Failed to list storage contents: {e}")
        sys.exit(1)


@files.command("download")
@click.argument("remote_path")
@click.argument("local_path")
@click.option("--region", "-r", required=True, help="AWS region")
@click.option("--namespace", "-n", default="gco-jobs", help="Kubernetes namespace")
@click.option(
    "--storage-type",
    "-t",
    type=click.Choice(["efs", "fsx"]),
    default="efs",
    help="Storage type (default: efs)",
)
@click.option("--pvc", help="PVC name (default: gco-shared-storage for EFS)")
@pass_config
def download_files(
    config: Any,
    remote_path: Any,
    local_path: Any,
    region: Any,
    namespace: Any,
    storage_type: Any,
    pvc: Any,
) -> None:
    """Download files from EFS/FSx storage.

    Creates a temporary helper pod to mount the storage and copy files,
    then cleans up automatically. Works even after job pods are deleted.

    REMOTE_PATH is relative to the storage root (e.g., efs-output-example/results.json)

    REQUIREMENTS:
    - kubectl installed and in PATH
    - EKS access entry configured for your IAM principal
    - AWS credentials with eks:DescribeCluster permission

    Examples:
        gco files download efs-output-example/results.json ./results.json -r us-east-1
        gco files download my-job/outputs ./outputs -r us-east-1
        gco files download checkpoints ./checkpoints -r us-west-2 -t fsx
    """
    formatter = get_output_formatter(config)
    fs_client = get_file_system_client(config)

    try:
        formatter.print_info(
            f"Downloading {remote_path} from {storage_type.upper()} to {local_path}..."
        )

        result = fs_client.download_from_storage(
            region=region,
            remote_path=remote_path,
            local_path=local_path,
            storage_type=storage_type,
            namespace=namespace,
            pvc_name=pvc,
        )

        formatter.print_success(f"Downloaded {result['size_bytes']} bytes to {local_path}")
        formatter.print(result)

    except Exception as e:
        formatter.print_error(f"Failed to download files: {e}")
        sys.exit(1)
