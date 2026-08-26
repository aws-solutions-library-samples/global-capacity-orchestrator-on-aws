"""File and object storage MCP tools."""

import asyncio
import json
from typing import Literal

import cli_runner
from audit import audit_logged
from feature_flags import FLAG_LOCAL_STORAGE_SYNC, FLAG_MODEL_UPLOAD, is_enabled
from local_data import LocalPathContract, resolve_local_path, stage_upload_path
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


@mcp.tool(tags={"safe", "storage"})
@audit_logged
async def s3_inventory(region: str | None = None) -> str:
    """Describe every S3 bucket the deployment creates, with its contract.

    Broader than list_storage_buckets, which returns only the four buckets
    addressable by ``storage sync``. This covers the always-on central
    (``Cluster_Shared_Bucket``) and per-region (``Regional_Shared_Bucket``)
    buckets, model weights, cost reports, the optional analytics Studio bucket,
    and every server-access-log sink.

    Each entry carries the owning stack and region, the bucket's purpose,
    reserved object-key prefixes, whether job pods have read-write / read-only /
    no access and how they discover the name (ConfigMap key or SSM path), the
    teardown removal policy, and whether it is currently deployed. Buckets whose
    stack is not deployed are included with ``status="not-deployed"`` so the
    inventory is complete rather than silently partial.

    Use this to answer "where can a job write?" — ``summary.pod_writable`` lists
    exactly the buckets the job-pod role can write to.

    Inventories buckets and their deployment contract; unrelated to the AWS
    "S3 Inventory" feature, which reports the objects inside a bucket.

    Args:
        region: Limit regional entries to one AWS region. Global, monitoring,
            and analytics entries are always included.
    """
    args = ["storage", "s3-inventory"]
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


if is_enabled(FLAG_MODEL_UPLOAD):

    @mcp.tool(tags={"data-upload", "storage", "local-filesystem"})
    @audit_logged
    async def upload_to_regional_bucket(
        local_path: str, region: str, prefix: str = "uploads"
    ) -> str:
        """[gated by GCO_ENABLE_MODEL_UPLOAD] Upload local data to a regional bucket.

        The source must resolve beneath ``GCO_STORAGE_LOCAL_ROOT``. Relative
        paths such as ``model.bin`` and ``./datasets`` are interpreted relative
        to that root, never relative to the MCP process working directory. The
        CLI receives a private descriptor-backed snapshot; descendant links,
        special files, hard links, and filesystem crossings fail closed.

        Args:
            local_path: Root-relative local file or directory to upload.
            region: Target region whose regional bucket receives the objects.
            prefix: S3 prefix for uploaded objects (default: ``uploads``).
        """
        try:
            local_contract = _resolve_upload_local_path(local_path)
        except (OSError, ValueError) as exc:
            return json.dumps({"error": str(exc), "code": "local_data_path_rejected"})

        def _upload_from_staged_path() -> str:
            with stage_upload_path(local_contract) as staged:
                return cli_runner._run_cli(
                    "models",
                    "upload-regional",
                    staged.argument,
                    "-r",
                    region,
                    "--prefix",
                    prefix,
                    pass_fds=(staged.directory_fd,),
                )

        try:
            # Run the staging context in the worker so cancellation cannot
            # unlink its descriptor-backed snapshot while the CLI still reads.
            return await asyncio.to_thread(_upload_from_staged_path)
        except (OSError, ValueError) as exc:
            return json.dumps({"error": str(exc), "code": "local_data_path_rejected"})


# Backward-compatible private name retained for focused tests and callers.
_SyncLocalContract = LocalPathContract


def _resolve_sync_local_path(
    local_path: str,
    *,
    require_exists: bool,
) -> LocalPathContract:
    """Issue an identity-bound confinement contract for an MCP sync path."""
    return resolve_local_path(
        local_path,
        require_exists=require_exists,
        purpose="Local sync",
    )


def _resolve_upload_local_path(local_path: str) -> LocalPathContract:
    """Resolve an existing short-upload source beneath the shared local root."""
    return resolve_local_path(local_path, require_exists=True, purpose="Local upload")


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
