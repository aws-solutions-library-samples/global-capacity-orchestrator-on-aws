"""Commands for discovering and syncing GCO S3 buckets."""

import signal
import sys
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from types import FrameType
from typing import Any

import click

from ..config import GCOConfig
from ..output import get_output_formatter

pass_config = click.make_pass_decorator(GCOConfig, ensure=True)


class _StorageSyncTerminated(RuntimeError):
    """Raised on SIGTERM so managed S3 transfers can unwind cooperatively."""


@contextmanager
def _cooperative_storage_sigterm() -> Iterator[None]:
    """Turn SIGTERM into an exception while a storage transfer is active."""
    if threading.current_thread() is not threading.main_thread():
        yield
        return

    previous_handler = signal.getsignal(signal.SIGTERM)

    def terminate_handler(signum: int, frame: FrameType | None) -> None:
        raise _StorageSyncTerminated(
            "Storage sync was terminated; in-progress managed transfers were cancelled"
        )

    signal.signal(signal.SIGTERM, terminate_handler)
    try:
        yield
    finally:
        signal.signal(signal.SIGTERM, previous_handler)


@click.group()
@pass_config
def storage(config: Any) -> None:
    """Discover and sync user-facing GCO S3 buckets."""
    pass


@storage.command("list")
@click.option(
    "--region",
    "-r",
    help="Limit regional-bucket discovery to this region",
)
@pass_config
def storage_list(config: Any, region: str | None) -> None:
    """List deployed buckets and the aliases accepted by `storage sync`.

    Examples:
        gco storage list
        gco storage list --region us-east-1
        gco --output json storage list
    """
    from ..storage import get_storage_manager

    formatter = get_output_formatter(config)
    try:
        buckets = get_storage_manager(config).list_buckets(region=region)
        if not buckets:
            if config.output_format == "table":
                formatter.print_info("No user-facing GCO S3 buckets were discovered")
            else:
                formatter.print([])
            return
        formatter.print(
            buckets,
            columns=["alias", "scope", "region", "bucket", "purpose", "s3_uri"],
        )
    except Exception as exc:
        formatter.print_error(f"Failed to discover GCO S3 buckets: {exc}")
        sys.exit(1)


@storage.command("sync")
@click.argument("bucket_alias")
@click.argument("local_dir", metavar="LOCAL_PATH")
@click.option(
    "--direction",
    type=click.Choice(["download", "upload"], case_sensitive=False),
    default="download",
    show_default=True,
    help="Transfer direction: S3 to local or local to S3",
)
@click.option(
    "--region",
    "-r",
    help="Region for the unqualified regional-shared alias",
)
@click.option(
    "--prefix",
    default="",
    help="Remote S3 key prefix to download from or upload into",
)
@click.option(
    "--dry-run",
    is_flag=True,
    help="Show a summary of the planned transfer without writing local files or S3 objects",
)
@click.option(
    "--force",
    is_flag=True,
    help="Transfer every file even when the destination appears current",
)
@click.option(
    "--_gco-storage-root",
    "confinement_root",
    hidden=True,
)
@click.option(
    "--_gco-storage-root-device",
    "confinement_device",
    type=int,
    hidden=True,
)
@click.option(
    "--_gco-storage-root-inode",
    "confinement_inode",
    type=int,
    hidden=True,
)
@pass_config
def storage_sync(
    config: Any,
    bucket_alias: str,
    local_dir: str,
    direction: str,
    region: str | None,
    prefix: str,
    dry_run: bool,
    force: bool,
    confinement_root: str | None,
    confinement_device: int | None,
    confinement_inode: int | None,
) -> None:
    """Sync between a GCO S3 bucket and LOCAL_PATH in one direction.

    BUCKET_ALIAS is one of cluster-shared, model-weights,
    analytics-studio, or regional-shared:REGION. The unqualified
    regional-shared alias can be paired with --region.

    The default direction downloads from S3. Use --direction upload to send a
    local file or directory to S3. Neither direction deletes destination-only
    files or objects; there is no automatic two-way conflict resolution.

    Examples:
        gco storage sync cluster-shared ./cluster-data
        gco storage sync regional-shared:us-east-1 ./regional-data
        gco storage sync regional-shared ./regional-data -r us-east-1
        gco storage sync model-weights ./models --prefix models/llama3
        gco storage sync cluster-shared ./results --direction upload --prefix results
    """
    from ..storage import get_storage_manager

    formatter = get_output_formatter(config)
    try:
        if config.output_format == "table":
            if dry_run:
                action = f"Planning {direction}"
            else:
                action = "Downloading" if direction == "download" else "Uploading"
            formatter.print_info(f"{action} for '{bucket_alias}'...")

        with _cooperative_storage_sigterm():
            result = get_storage_manager(config).sync(
                bucket_alias,
                local_dir,
                region=region,
                prefix=prefix,
                direction=direction,
                dry_run=dry_run,
                force=force,
                confinement_root=confinement_root,
                confinement_device=confinement_device,
                confinement_inode=confinement_inode,
            )

        if config.output_format != "table":
            formatter.print(result)
            return

        result_direction = result["direction"]
        transfer_verb = "downloaded" if result_direction == "download" else "uploaded"
        if dry_run:
            formatter.print_success(
                f"Dry run: {result['files_planned']} file(s) "
                f"({result['bytes_planned']} bytes) would be {transfer_verb}"
            )
        else:
            files_key = "files_downloaded" if result_direction == "download" else "files_uploaded"
            bytes_key = "bytes_downloaded" if result_direction == "download" else "bytes_uploaded"
            formatter.print_success(
                f"{transfer_verb.capitalize()} {result[files_key]} file(s) "
                f"({result[bytes_key]} bytes)"
            )
        formatter.print_info(f"Source: {result['source']}")
        formatter.print_info(f"Destination: {result['destination']}")
        if result["files_skipped"]:
            formatter.print_info(f"Skipped {result['files_skipped']} current file(s)")
    except Exception as exc:
        formatter.print_error(f"Failed to sync GCO S3 bucket: {exc}")
        sys.exit(1)
