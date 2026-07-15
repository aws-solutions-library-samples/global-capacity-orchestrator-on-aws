"""File and object storage MCP tools."""

import asyncio
import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import cli_runner
from audit import audit_logged
from feature_flags import FLAG_LOCAL_STORAGE_SYNC, is_enabled
from server import mcp


@mcp.tool(tags={"safe", "storage"})
@audit_logged
def list_storage_contents(region: str, path: str = "/") -> str:
    """List contents of shared EFS storage.

    Args:
        region: AWS region.
        path: Directory path to list (default: root).
    """
    args = ["files", "ls", "-r", region]
    if path != "/":
        args.append(path)
    return cli_runner._run_cli(*args)


@mcp.tool(tags={"safe", "storage"})
@audit_logged
def list_file_systems(region: str | None = None) -> str:
    """List EFS and FSx file systems.

    Args:
        region: Specific region, or omit for all.
    """
    args = ["files", "list"]
    if region:
        args += ["-r", region]
    return cli_runner._run_cli(*args)


@mcp.tool(tags={"safe", "storage"})
@audit_logged
async def list_storage_buckets(region: str | None = None) -> str:
    """List deployed GCO S3 buckets and their human-friendly aliases.

    Returns user-facing buckets such as ``cluster-shared``, ``model-weights``,
    ``regional-shared:<region>``, and the optional ``analytics-studio`` bucket.
    Physical names are resolved from the deployment's SSM and CloudFormation
    metadata rather than reconstructed.

    Args:
        region: Optionally limit regional-bucket discovery to one AWS region.
            Global and analytics buckets are still included.
    """
    args = ["storage", "list"]
    if region:
        args += ["--region", region]
    return await asyncio.to_thread(cli_runner._run_cli, *args)


# =============================================================================
# Read-only inspection tools (async)
# =============================================================================


@mcp.tool(tags={"safe", "files"})
@audit_logged
async def files_get(region: str, fs_type: str = "efs") -> str:
    """`gco files get` — get file system details for a region.

    Returns the EFS (or FSx) file system's ID, lifecycle state, throughput mode,
    encryption flags, and mount targets. To browse or fetch file contents, use
    list_storage_contents (``gco files ls``) or ``gco files download``.

    Args:
        region: AWS region.
        fs_type: File system type — "efs" (default) or "fsx".
    """
    return await asyncio.to_thread(cli_runner._run_cli, "files", "get", region, "-t", fs_type)


@mcp.tool(tags={"safe", "files"})
@audit_logged
async def files_access_points(region: str | None = None) -> str:
    """`gco files access-points` — list EFS access points.

    Args:
        region: AWS region.
    """
    args = ["files", "access-points"]
    if region:
        args += ["-r", region]
    return await asyncio.to_thread(cli_runner._run_cli, *args)


# =============================================================================
# Regional bucket upload (low-risk write)
# =============================================================================


@mcp.tool(tags={"low-risk", "storage"})
@audit_logged
async def upload_to_regional_bucket(local_path: str, region: str, prefix: str = "uploads") -> str:
    """`gco models upload-regional` — upload local files to a region's regional bucket.

    Objects are written to the target region's general-purpose
    gco-regional-shared-<account>-<region> bucket, resolved from that region's
    own SSM parameter. The bucket is general purpose and usable by any
    in-region workload.

    Args:
        local_path: Local file or directory to upload.
        region: Target region whose regional bucket receives the objects.
        prefix: S3 prefix for uploaded objects (default: "uploads").
    """
    return await asyncio.to_thread(
        cli_runner._run_cli,
        "models",
        "upload-regional",
        local_path,
        "-r",
        region,
        "--prefix",
        prefix,
    )


@dataclass(frozen=True)
class _SyncLocalContract:
    """Root-relative argument plus the root identity the CLI must pin."""

    local_argument: str
    root: Path
    device: int
    inode: int


def _resolve_sync_local_path(
    local_path: str,
    *,
    require_exists: bool,
) -> _SyncLocalContract:
    """Issue an identity-bound confinement contract for an MCP sync path."""
    configured_root = os.environ.get("GCO_STORAGE_LOCAL_ROOT", "").strip()
    if not configured_root:
        raise ValueError("GCO_STORAGE_LOCAL_ROOT must be set before enabling local storage sync")
    if os.name != "posix" or not hasattr(os, "O_NOFOLLOW") or not hasattr(os, "O_DIRECTORY"):
        raise ValueError(
            "Local storage sync requires descriptor-relative no-follow filesystem support"
        )

    root = Path(configured_root).expanduser().resolve(strict=True)
    root_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    root_fd = os.open(root, root_flags)
    try:
        root_stat = os.fstat(root_fd)
    finally:
        os.close(root_fd)
    if not stat.S_ISDIR(root_stat.st_mode):
        raise ValueError(f"GCO_STORAGE_LOCAL_ROOT is not a directory: {root}")

    supplied = Path(local_path).expanduser()
    candidate = supplied if supplied.is_absolute() else root / supplied
    lexical = Path(os.path.abspath(candidate))
    try:
        relative = lexical.relative_to(root)
    except ValueError as exc:
        raise ValueError(
            f"Local sync path must stay within GCO_STORAGE_LOCAL_ROOT: {local_path}"
        ) from exc

    resolved = lexical.resolve()
    if not resolved.is_relative_to(root):
        raise ValueError(f"Local sync path must stay within GCO_STORAGE_LOCAL_ROOT: {local_path}")
    if require_exists and not lexical.exists():
        raise ValueError(f"Local upload source does not exist: {local_path}")

    return _SyncLocalContract(
        local_argument=str(relative) if relative.parts else ".",
        root=root,
        device=root_stat.st_dev,
        inode=root_stat.st_ino,
    )


# Storage sync reads or writes the MCP host's filesystem and may transfer a
# large amount of data, so the tool is absent unless the operator opts in.
if is_enabled(FLAG_LOCAL_STORAGE_SYNC):

    @mcp.tool(tags={"low-risk", "storage", "local-filesystem", "data-upload"})
    @audit_logged
    async def sync_storage_bucket(
        bucket_alias: str,
        local_dir: str,
        direction: Literal["download", "upload"] = "download",
        region: str | None = None,
        prefix: str = "",
        dry_run: bool = False,
        force: bool = False,
    ) -> str:
        """Sync between a GCO S3 bucket and the MCP host in one direction.

        [gated by GCO_ENABLE_LOCAL_STORAGE_SYNC] The local path is confined
        beneath ``GCO_STORAGE_LOCAL_ROOT`` before the CLI is invoked. This
        confinement requires POSIX descriptor-relative filesystem APIs and
        fails closed on unsupported hosts. Download is the default; upload
        reads a local file or directory and writes S3. Neither direction
        deletes destination-only data.

        Args:
            bucket_alias: Human-friendly alias returned by
                ``list_storage_buckets``.
            local_dir: Local path relative to ``GCO_STORAGE_LOCAL_ROOT`` (or an
                absolute path contained by that root).
            direction: ``download`` for S3-to-local or ``upload`` for local-to-S3.
            region: Region for an unqualified ``regional-shared`` alias.
            prefix: Remote S3 key prefix to download from or upload into.
            dry_run: Return the transfer summary without writing files or S3 objects.
            force: Transfer all matching files even if the destination is current.
        """
        normalized_direction = direction.strip().lower()
        try:
            local_contract = _resolve_sync_local_path(
                local_dir,
                require_exists=normalized_direction == "upload",
            )
        except (OSError, ValueError) as exc:
            return json.dumps(
                {
                    "error": str(exc),
                    "code": "local_storage_path_rejected",
                }
            )

        args = [
            "storage",
            "sync",
            "--direction",
            normalized_direction,
            "--_gco-storage-root",
            str(local_contract.root),
            "--_gco-storage-root-device",
            str(local_contract.device),
            "--_gco-storage-root-inode",
            str(local_contract.inode),
        ]
        if region:
            args += ["--region", region]
        if prefix:
            args += ["--prefix", prefix]
        if dry_run:
            args.append("--dry-run")
        if force:
            args.append("--force")
        # End option parsing before untrusted positional values. In particular,
        # a confined root child named ``--prefix`` must remain a local path.
        args += ["--", bucket_alias, local_contract.local_argument]
        return await cli_runner._run_cli_async(
            *args,
            timeout_seconds=3600,
            terminate_grace_seconds=30,
        )
