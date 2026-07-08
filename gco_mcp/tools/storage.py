"""File storage MCP tools."""

import asyncio

import cli_runner
from audit import audit_logged
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
