"""
File system operations for GCO CLI.

Provides functionality to interact with EFS and FSx for Lustre file systems
attached to GCO regional stacks.
"""

import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

import boto3
from botocore.exceptions import ClientError

from ._image_uri import aws_partition, aws_url_suffix
from .aws_client import get_aws_client
from .config import GCOConfig, get_config
from .kubectl_helpers import update_kubeconfig

_DNS_LABEL_RE = re.compile(r"[a-z0-9](?:[-a-z0-9]*[a-z0-9])?")
_DNS_SUBDOMAIN_RE = re.compile(
    r"[a-z0-9](?:[-a-z0-9]*[a-z0-9])?(?:\.[a-z0-9](?:[-a-z0-9]*[a-z0-9])?)*"
)


def _validated_kubernetes_name(
    value: str,
    field: str,
    *,
    allow_subdomains: bool,
) -> str:
    """Return a Kubernetes name after strict DNS syntax validation."""
    max_length = 253 if allow_subdomains else 63
    pattern = _DNS_SUBDOMAIN_RE if allow_subdomains else _DNS_LABEL_RE
    if not isinstance(value, str) or not value or len(value) > max_length:
        raise ValueError(f"{field} must be a non-empty Kubernetes DNS name")
    if pattern.fullmatch(value) is None:
        raise ValueError(f"{field} contains characters not allowed in a Kubernetes DNS name")
    return value


def _validated_copy_path(value: str, field: str) -> str:
    """Reject empty paths and control characters before passing them to kubectl."""
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field} must be a non-empty path")
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError(f"{field} must not contain control characters")
    return value


def _validated_local_destination(local_path: str) -> str:
    """Ensure kubectl cannot reinterpret the local destination as a pod spec."""
    local_path = _validated_copy_path(local_path, "local_path")
    if ":" in local_path:
        raise ValueError("local_path must not contain ':' because kubectl treats it as a pod path")
    return local_path


def _create_local_destination_parent(local_path: str) -> None:
    """Create only the parent kubectl needs, preserving leaf copy semantics."""
    Path(local_path).parent.mkdir(parents=True, exist_ok=True)


def _validated_pod_remote_path(remote_path: str) -> str:
    """Require an absolute pod path so remote tar sees no ambiguous input."""
    remote_path = _validated_copy_path(remote_path, "remote_path")
    if "\\" in remote_path:
        raise ValueError("remote_path must not contain backslashes")
    if not remote_path.startswith("/"):
        raise ValueError("remote_path must be absolute when copying from a pod")
    return remote_path


def _storage_sub_path(remote_path: str) -> str | None:
    """Return a safe PVC subPath, or ``None`` when the root was requested."""
    remote_path = _validated_copy_path(remote_path, "remote_path")
    if "\\" in remote_path:
        raise ValueError("remote_path must not contain backslashes")
    relative = remote_path.lstrip("/")
    if not relative:
        return None
    if any(part in (".", "..") for part in relative.split("/")):
        raise ValueError("remote_path must stay beneath the mounted storage root")
    return str(PurePosixPath(relative))


def _storage_remote_path(mount_path: str, remote_path: str) -> str:
    """Resolve a user path beneath a helper pod's storage mount.

    The same validated relative path is mounted with Kubernetes ``subPath`` in
    the helper pod. Kubelet's symlink-safe subPath resolution makes the runtime
    boundary stronger than a lexical ``..`` check alone.
    """
    sub_path = _storage_sub_path(remote_path)
    if sub_path is None:
        return mount_path
    return str(PurePosixPath(mount_path).joinpath(sub_path))


def _helper_pod_manifest(
    *,
    pod_name: str,
    namespace: str,
    pvc_name: str,
    mount_path: str,
    app_label: str,
    storage_sub_path: str | None = None,
) -> str:
    """Build a JSON Kubernetes manifest without YAML string interpolation."""
    volume_mount: dict[str, Any] = {
        "name": "storage",
        "mountPath": mount_path,
        "readOnly": True,
    }
    if storage_sub_path is not None:
        volume_mount["subPath"] = storage_sub_path
    manifest = {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {
            "name": pod_name,
            "namespace": namespace,
            "labels": {"app": app_label},
        },
        "spec": {
            "automountServiceAccountToken": False,
            "enableServiceLinks": False,
            "restartPolicy": "Never",
            "containers": [
                {
                    "name": "helper",
                    "image": "busybox:1.38.0",
                    "command": ["sleep", "300"],
                    "resources": {
                        "requests": {"cpu": "50m", "memory": "64Mi"},
                        "limits": {"cpu": "200m", "memory": "256Mi"},
                    },
                    "volumeMounts": [volume_mount],
                }
            ],
            "volumes": [
                {
                    "name": "storage",
                    "persistentVolumeClaim": {"claimName": pvc_name},
                }
            ],
        },
    }
    return json.dumps(manifest)


@dataclass
class FileSystemInfo:
    """Information about a file system."""

    file_system_id: str
    file_system_type: str  # "efs" or "fsx"
    region: str
    dns_name: str
    mount_target_ip: str | None = None
    size_bytes: int | None = None
    status: str = "available"
    created_time: datetime | None = None
    tags: dict[str, str] | None = None

    def __post_init__(self) -> None:
        if self.tags is None:
            self.tags = {}


@dataclass
class FileInfo:
    """Information about a file or directory."""

    path: str
    name: str
    is_directory: bool
    size_bytes: int = 0
    modified_time: datetime | None = None
    owner: str | None = None


class FileSystemClient:
    """
    Client for interacting with GCO file systems.

    Supports:
    - Listing file systems (EFS/FSx) in GCO stacks
    - Getting file system information and access points
    - Downloading files from pods via kubectl cp
    """

    def __init__(self, config: GCOConfig | None = None):
        self.config = config or get_config()
        self._session = boto3.Session()
        self._aws_client = get_aws_client(config)

    def get_file_systems(self, region: str | None = None) -> list[FileSystemInfo]:
        """
        Get all file systems associated with GCO stacks.

        Args:
            region: Specific region to query (None for all regions)

        Returns:
            List of FileSystemInfo objects
        """
        file_systems = []

        # Get regional stacks
        stacks = self._aws_client.discover_regional_stacks()

        if region:
            stacks = {k: v for k, v in stacks.items() if k == region}

        for stack_region, stack in stacks.items():
            # Get EFS file systems
            if stack.efs_file_system_id:
                efs_info = self._get_efs_info(stack.efs_file_system_id, stack_region)
                if efs_info:
                    file_systems.append(efs_info)

            # Get FSx file systems
            if stack.fsx_file_system_id:
                fsx_info = self._get_fsx_info(stack.fsx_file_system_id, stack_region)
                if fsx_info:
                    file_systems.append(fsx_info)

        return file_systems

    def _get_efs_info(self, file_system_id: str, region: str) -> FileSystemInfo | None:
        """Get information about an EFS file system."""
        try:
            efs = self._session.client("efs", region_name=region)

            response = efs.describe_file_systems(FileSystemId=file_system_id)
            if not response["FileSystems"]:
                return None

            fs = response["FileSystems"][0]

            # Get mount targets for DNS name
            mt_response = efs.describe_mount_targets(FileSystemId=file_system_id)
            mount_target_ip = None
            if mt_response["MountTargets"]:
                mount_target_ip = mt_response["MountTargets"][0].get("IpAddress")

            # Get tags
            tags_response = efs.describe_tags(FileSystemId=file_system_id)
            tags = {t["Key"]: t["Value"] for t in tags_response.get("Tags", [])}

            return FileSystemInfo(
                file_system_id=file_system_id,
                file_system_type="efs",
                region=region,
                dns_name=f"{file_system_id}.efs.{region}.{aws_url_suffix(region)}",
                mount_target_ip=mount_target_ip,
                size_bytes=fs.get("SizeInBytes", {}).get("Value"),
                status=fs["LifeCycleState"],
                created_time=fs.get("CreationTime"),
                tags=tags,
            )
        except ClientError:
            return None

    def _get_fsx_info(self, file_system_id: str, region: str) -> FileSystemInfo | None:
        """Get information about an FSx for Lustre file system."""
        try:
            fsx = self._session.client("fsx", region_name=region)

            response = fsx.describe_file_systems(FileSystemIds=[file_system_id])
            if not response["FileSystems"]:
                return None

            fs = response["FileSystems"][0]

            # Get DNS name from Lustre configuration
            dns_name = fs.get("DNSName", "")

            # Get tags
            tags = {t["Key"]: t["Value"] for t in fs.get("Tags", [])}

            return FileSystemInfo(
                file_system_id=file_system_id,
                file_system_type="fsx",
                region=region,
                dns_name=dns_name,
                size_bytes=fs.get("StorageCapacity", 0) * 1024 * 1024 * 1024,  # GB to bytes
                status=fs["Lifecycle"],
                created_time=fs.get("CreationTime"),
                tags=tags,
            )
        except ClientError:
            return None

    def get_file_system_by_region(self, region: str, fs_type: str = "efs") -> FileSystemInfo | None:
        """
        Get file system for a specific region.

        Args:
            region: AWS region
            fs_type: "efs" or "fsx"

        Returns:
            FileSystemInfo or None
        """
        file_systems = self.get_file_systems(region)
        for fs in file_systems:
            if fs.file_system_type == fs_type:
                return fs
        return None

    def create_datasync_download_task(
        self,
        file_system_id: str,
        region: str,
        source_path: str,
        destination_bucket: str,
        destination_prefix: str = "",
    ) -> str:
        """
        Create a DataSync task to download files from EFS/FSx to S3.

        This is useful for downloading large amounts of data from file systems
        that aren't directly accessible.

        Args:
            file_system_id: EFS or FSx file system ID
            region: AWS region
            source_path: Path within the file system
            destination_bucket: S3 bucket name
            destination_prefix: S3 key prefix

        Returns:
            DataSync task ARN
        """
        datasync = self._session.client("datasync", region_name=region)
        partition = aws_partition(region)

        # Determine file system type
        fs_info = None
        for fs in self.get_file_systems(region):
            if fs.file_system_id == file_system_id:
                fs_info = fs
                break

        if not fs_info:
            raise ValueError(f"File system {file_system_id} not found in region {region}")

        # Create source location
        if fs_info.file_system_type == "efs":
            source_location = datasync.create_location_efs(
                EfsFilesystemArn=f"arn:{partition}:elasticfilesystem:{region}:{self._get_account_id()}:file-system/{file_system_id}",
                Subdirectory=source_path,
                Ec2Config={
                    "SubnetArn": self._get_subnet_arn(region),
                    "SecurityGroupArns": [self._get_security_group_arn(region)],
                },
            )
            source_arn = source_location["LocationArn"]
        else:
            source_location = datasync.create_location_fsx_lustre(
                FsxFilesystemArn=f"arn:{partition}:fsx:{region}:{self._get_account_id()}:file-system/{file_system_id}",
                Subdirectory=source_path,
                SecurityGroupArns=[self._get_security_group_arn(region)],
            )
            source_arn = source_location["LocationArn"]

        # Create destination location (S3)
        dest_location = datasync.create_location_s3(
            S3BucketArn=f"arn:{partition}:s3:::{destination_bucket}",
            Subdirectory=destination_prefix,
            S3Config={"BucketAccessRoleArn": self._get_datasync_role_arn(region)},
        )
        dest_arn = dest_location["LocationArn"]

        # Create task
        task = datasync.create_task(
            SourceLocationArn=source_arn,
            DestinationLocationArn=dest_arn,
            Name=f"gco-download-{datetime.now(UTC).strftime('%Y%m%d-%H%M%S')}",
            Options={
                "VerifyMode": "ONLY_FILES_TRANSFERRED",
                "OverwriteMode": "ALWAYS",
                "PreserveDeletedFiles": "REMOVE",
                "TransferMode": "CHANGED",
            },
        )

        return str(task["TaskArn"])

    def _get_account_id(self) -> str:
        """Get current AWS account ID."""
        sts = self._session.client("sts")
        return str(sts.get_caller_identity()["Account"])

    def _get_subnet_arn(self, _region: str) -> str:
        """Get a subnet ARN for DataSync in the given region."""
        # This would need to be implemented based on your VPC setup
        # For now, return a placeholder
        raise NotImplementedError("Subnet ARN lookup not implemented - configure via stack outputs")

    def _get_security_group_arn(self, _region: str) -> str:
        """Get a security group ARN for DataSync in the given region."""
        raise NotImplementedError(
            "Security group ARN lookup not implemented - configure via stack outputs"
        )

    def _get_datasync_role_arn(self, region: str) -> str:
        """Get the DataSync IAM role ARN."""
        raise NotImplementedError(
            "DataSync role ARN lookup not implemented - configure via stack outputs"
        )

    def get_access_point_info(self, file_system_id: str, region: str) -> list[dict[str, Any]]:
        """
        Get EFS access points for a file system.

        Args:
            file_system_id: EFS file system ID
            region: AWS region

        Returns:
            List of access point information
        """
        try:
            efs = self._session.client("efs", region_name=region)
            response = efs.describe_access_points(FileSystemId=file_system_id)

            return [
                {
                    "access_point_id": ap["AccessPointId"],
                    "name": ap.get("Name", ""),
                    "path": ap.get("RootDirectory", {}).get("Path", "/"),
                    "posix_user": ap.get("PosixUser", {}),
                    "status": ap["LifeCycleState"],
                }
                for ap in response.get("AccessPoints", [])
            ]
        except ClientError:
            return []

    def download_from_pod(
        self,
        region: str,
        pod_name: str,
        remote_path: str,
        local_path: str,
        namespace: str = "gco-jobs",
        container: str | None = None,
    ) -> dict[str, Any]:
        """
        Download files from a pod using kubectl cp.

        This uses kubectl port-forward internally to copy files from a pod's
        mounted file system (EFS/FSx) to the local machine.

        Args:
            region: AWS region where the cluster is located
            pod_name: Name of the pod to copy from
            remote_path: Path inside the pod (e.g., /mnt/efs/outputs)
            local_path: Local destination path
            namespace: Kubernetes namespace (default: gco-jobs)
            container: Container name (optional, for multi-container pods)

        Returns:
            Dict with download status and details
        """
        import os
        import subprocess

        namespace = _validated_kubernetes_name(namespace, "namespace", allow_subdomains=False)
        pod_name = _validated_kubernetes_name(pod_name, "pod_name", allow_subdomains=True)
        remote_path = _validated_pod_remote_path(remote_path)
        local_path = _validated_local_destination(local_path)
        if container is not None:
            container = _validated_kubernetes_name(container, "container", allow_subdomains=False)
        _create_local_destination_parent(local_path)

        # Update kubeconfig for the cluster
        cluster_name = f"{self.config.project_name}-{region}"
        update_kubeconfig(cluster_name, region)

        # Build kubectl cp command
        # Format: kubectl cp <namespace>/<pod>:<remote_path> <local_path>
        source = f"{namespace}/{pod_name}:{remote_path}"
        cmd = ["kubectl", "cp"]

        if container:
            cmd.extend(["-c", container])
        cmd.extend(["--", source, local_path])

        try:
            subprocess.run(
                cmd, check=True, capture_output=True, text=True
            )  # nosemgrep: dangerous-subprocess-use-audit - cmd is a list ["kubectl","cp",source,local_path]; source is namespace/pod:path, local_path is caller-provided destination
            if os.path.isfile(local_path):
                size = os.path.getsize(local_path)
            elif os.path.isdir(local_path):
                size = sum(
                    os.path.getsize(os.path.join(dirpath, filename))
                    for dirpath, _, filenames in os.walk(local_path)
                    for filename in filenames
                )
            else:
                raise RuntimeError(
                    "kubectl cp reported success but did not create the local destination: "
                    f"{local_path}"
                )

            return {
                "status": "success",
                "source": source,
                "destination": local_path,
                "size_bytes": size,
                "message": "Download completed successfully",
            }

        except subprocess.CalledProcessError as e:
            raise RuntimeError(f"kubectl cp failed: {e.stderr}") from e
        except FileNotFoundError as e:
            raise RuntimeError(
                "kubectl not found. Please install kubectl and ensure it's in your PATH."
            ) from e

    def list_storage_contents(
        self,
        region: str,
        remote_path: str = "/",
        storage_type: str = "efs",
        namespace: str = "gco-jobs",
        pvc_name: str | None = None,
    ) -> dict[str, Any]:
        """
        List contents of EFS/FSx storage using a temporary helper pod.

        This creates a temporary pod that mounts the storage, lists contents,
        then cleans up. Useful for discovering what directories/files exist.

        Args:
            region: AWS region where the cluster is located
            remote_path: Path inside the storage to list (default: root)
            storage_type: "efs" or "fsx" (default: efs)
            namespace: Kubernetes namespace (default: gco-jobs)
            pvc_name: PVC name to mount (default: gco-shared-storage for EFS,
                      gco-fsx-storage for FSx)

        Returns:
            Dict with listing status and contents
        """
        import subprocess
        import time
        import uuid

        if storage_type not in ("efs", "fsx"):
            raise ValueError("storage_type must be 'efs' or 'fsx'")
        namespace = _validated_kubernetes_name(namespace, "namespace", allow_subdomains=False)

        # Determine PVC name based on storage type
        if pvc_name is None:
            pvc_name = "gco-shared-storage" if storage_type == "efs" else "gco-fsx-storage"
        pvc_name = _validated_kubernetes_name(pvc_name, "pvc_name", allow_subdomains=True)

        # Determine mount path based on storage type
        mount_path = "/efs" if storage_type == "efs" else "/fsx"
        storage_sub_path = _storage_sub_path(remote_path)
        full_remote_path = _storage_remote_path(mount_path, remote_path)

        # Generate unique pod name
        helper_pod_name = f"gco-list-helper-{uuid.uuid4().hex[:8]}"

        # Update kubeconfig for the cluster
        cluster_name = f"{self.config.project_name}-{region}"
        update_kubeconfig(cluster_name, region)

        # Create helper pod manifest.
        #
        # We set an explicit ``resources`` block with CPU + memory but no GPU
        # so the gco-jobs LimitRange admission plugin does not substitute its
        # ``max`` value as an implicit request. Without this, a namespace
        # that already has all 32 GPUs in use (typical during a demo or
        # heavy workload burst) would reject the helper pod — even though
        # listing files doesn't need a GPU — because K8s quota admission
        # would attribute the LimitRange's ``max.nvidia.com/gpu`` to the
        # pod's request.
        pod_manifest = _helper_pod_manifest(
            pod_name=helper_pod_name,
            namespace=namespace,
            pvc_name=pvc_name,
            mount_path=full_remote_path,
            app_label="gco-list-helper",
            storage_sub_path=storage_sub_path,
        )

        try:
            # Create the helper pod
            subprocess.run(
                ["kubectl", "apply", "-f", "-"],
                input=pod_manifest,
                capture_output=True,
                text=True,
                check=True,
            )

            # Wait for pod to be ready
            max_wait = 60
            waited = 0
            while waited < max_wait:
                status_result = subprocess.run(
                    [
                        "kubectl",
                        "get",
                        "pod",
                        helper_pod_name,
                        "-n",
                        namespace,
                        "-o",
                        "jsonpath={.status.phase}",
                    ],
                    capture_output=True,
                    text=True,
                )
                if status_result.stdout.strip() == "Running":
                    break
                time.sleep(2)  # nosemgrep: arbitrary-sleep
                waited += 2

            if waited >= max_wait:
                raise RuntimeError("Helper pod did not become ready in time")

            # List contents using kubectl exec
            list_result = subprocess.run(
                [
                    "kubectl",
                    "exec",
                    helper_pod_name,
                    "-n",
                    namespace,
                    "--",
                    "ls",
                    "-la",
                    "--",
                    full_remote_path,
                ],
                capture_output=True,
                text=True,
            )

            if list_result.returncode != 0:
                return {
                    "status": "error",
                    "path": remote_path,
                    "storage_type": storage_type,
                    "contents": [],
                    "message": f"Path not found or empty: {list_result.stderr.strip()}",
                }

            # Parse ls output
            contents = []
            for line in list_result.stdout.strip().split("\n"):
                if line.startswith("total") or not line.strip():
                    continue
                parts = line.split()
                if len(parts) >= 9:
                    name = " ".join(parts[8:])
                    is_dir = line.startswith("d")
                    size = int(parts[4]) if parts[4].isdigit() else 0
                    contents.append(
                        {
                            "name": name,
                            "is_directory": is_dir,
                            "size_bytes": size,
                            "permissions": parts[0],
                        }
                    )

            return {
                "status": "success",
                "path": remote_path,
                "storage_type": storage_type,
                "contents": contents,
                "message": f"Found {len(contents)} items",
            }

        except subprocess.CalledProcessError as e:
            error_msg = e.stderr if e.stderr else str(e)
            raise RuntimeError(f"List failed: {error_msg}") from e
        except FileNotFoundError as e:
            raise RuntimeError(
                "kubectl not found. Please install kubectl and ensure it's in your PATH."
            ) from e
        finally:
            # Always clean up the helper pod
            import contextlib

            with contextlib.suppress(Exception):
                subprocess.run(
                    [
                        "kubectl",
                        "delete",
                        "pod",
                        helper_pod_name,
                        "-n",
                        namespace,
                        "--ignore-not-found",
                    ],
                    capture_output=True,
                    text=True,
                )

    def download_from_storage(
        self,
        region: str,
        remote_path: str,
        local_path: str,
        storage_type: str = "efs",
        namespace: str = "gco-jobs",
        pvc_name: str | None = None,
    ) -> dict[str, Any]:
        """
        Download files from EFS/FSx storage using a temporary helper pod.

        This creates a temporary pod that mounts the storage, copies files via
        kubectl cp, then cleans up. Works even after the original job pod is gone.

        Args:
            region: AWS region where the cluster is located
            remote_path: Path inside the storage (e.g., /efs-output-example/results.json)
            local_path: Local destination path
            storage_type: "efs" or "fsx" (default: efs)
            namespace: Kubernetes namespace (default: gco-jobs)
            pvc_name: PVC name to mount (default: gco-shared-storage for EFS,
                      gco-fsx-storage for FSx)

        Returns:
            Dict with download status and details
        """
        import os
        import subprocess
        import time
        import uuid

        if storage_type not in ("efs", "fsx"):
            raise ValueError("storage_type must be 'efs' or 'fsx'")
        namespace = _validated_kubernetes_name(namespace, "namespace", allow_subdomains=False)
        local_path = _validated_local_destination(local_path)

        # Determine PVC name based on storage type
        if pvc_name is None:
            pvc_name = "gco-shared-storage" if storage_type == "efs" else "gco-fsx-storage"
        pvc_name = _validated_kubernetes_name(pvc_name, "pvc_name", allow_subdomains=True)

        # Determine mount path based on storage type
        mount_path = "/efs" if storage_type == "efs" else "/fsx"
        storage_sub_path = _storage_sub_path(remote_path)
        full_remote_path = _storage_remote_path(mount_path, remote_path)
        _create_local_destination_parent(local_path)

        # Generate unique pod name
        helper_pod_name = f"gco-download-helper-{uuid.uuid4().hex[:8]}"

        # Update kubeconfig for the cluster
        cluster_name = f"{self.config.project_name}-{region}"
        update_kubeconfig(cluster_name, region)

        # Create helper pod manifest.
        #
        # Explicit ``resources`` block avoids the LimitRange admission plugin
        # substituting ``max.nvidia.com/gpu`` as an implicit request — see the
        # ``ls`` helper above for the full rationale.
        pod_manifest = _helper_pod_manifest(
            pod_name=helper_pod_name,
            namespace=namespace,
            pvc_name=pvc_name,
            mount_path=full_remote_path,
            app_label="gco-download-helper",
            storage_sub_path=storage_sub_path,
        )

        try:
            # Create the helper pod
            subprocess.run(
                ["kubectl", "apply", "-f", "-"],
                input=pod_manifest,
                capture_output=True,
                text=True,
                check=True,
            )

            # Wait for pod to be ready
            max_wait = 60
            waited = 0
            while waited < max_wait:
                status_result = subprocess.run(
                    [
                        "kubectl",
                        "get",
                        "pod",
                        helper_pod_name,
                        "-n",
                        namespace,
                        "-o",
                        "jsonpath={.status.phase}",
                    ],
                    capture_output=True,
                    text=True,
                )
                if status_result.stdout.strip() == "Running":
                    break
                time.sleep(2)  # nosemgrep: arbitrary-sleep
                waited += 2

            if waited >= max_wait:
                raise RuntimeError("Helper pod did not become ready in time")

            # Copy files from the helper pod
            source = f"{namespace}/{helper_pod_name}:{full_remote_path}"
            cmd = ["kubectl", "cp", "--", source, local_path]

            subprocess.run(
                cmd, check=True, capture_output=True, text=True
            )  # nosemgrep: dangerous-subprocess-use-audit - cmd is a list ["kubectl","cp",source,local_path]; source is namespace/pod:path, local_path is caller-provided destination

            # Get file info
            if os.path.isfile(local_path):
                size = os.path.getsize(local_path)
            elif os.path.isdir(local_path):
                size = sum(
                    os.path.getsize(os.path.join(dirpath, filename))
                    for dirpath, _, filenames in os.walk(local_path)
                    for filename in filenames
                )
            else:
                raise RuntimeError(
                    "kubectl cp reported success but did not create the local destination: "
                    f"{local_path}"
                )

            return {
                "status": "success",
                "source": f"{storage_type}:{remote_path}",
                "destination": local_path,
                "size_bytes": size,
                "storage_type": storage_type,
                "message": "Download completed successfully",
            }

        except subprocess.CalledProcessError as e:
            error_msg = e.stderr if e.stderr else str(e)
            raise RuntimeError(f"Download failed: {error_msg}") from e
        except FileNotFoundError as e:
            raise RuntimeError(
                "kubectl not found. Please install kubectl and ensure it's in your PATH."
            ) from e
        finally:
            # Always clean up the helper pod
            import contextlib

            with contextlib.suppress(Exception):
                subprocess.run(
                    [
                        "kubectl",
                        "delete",
                        "pod",
                        helper_pod_name,
                        "-n",
                        namespace,
                        "--ignore-not-found",
                    ],
                    capture_output=True,
                    text=True,
                )


def get_file_system_client(config: GCOConfig | None = None) -> FileSystemClient:
    """Get a configured file system client instance."""
    return FileSystemClient(config)
