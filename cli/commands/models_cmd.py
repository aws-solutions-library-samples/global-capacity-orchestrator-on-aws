"""Model weight management commands."""

import sys
from typing import Any

import click

from ..config import GCOConfig
from ..output import get_output_formatter

pass_config = click.make_pass_decorator(GCOConfig, ensure=True)


@click.group()
@pass_config
def models(config: Any) -> None:
    """Manage model weights in the central S3 bucket."""
    pass


@models.command("upload")
@click.argument("local_path")
@click.option("--name", "-n", required=True, help="Model name in the registry")
@pass_config
def models_upload(config: Any, local_path: Any, name: Any) -> None:
    """Upload model weights to the central S3 bucket.

    Models uploaded here are available to inference endpoints in all regions.
    The inference_monitor syncs them to local EFS via an init container.

    Examples:
        gco models upload ./my-model/ --name llama3-8b
        gco models upload ./weights.safetensors --name my-model
    """
    from ..models import get_model_manager

    formatter = get_output_formatter(config)

    try:
        manager = get_model_manager(config)
        formatter.print_info(f"Uploading {local_path} as '{name}'...")
        result = manager.upload(local_path, name)

        formatter.print_success(
            f"Uploaded {result['files_uploaded']} file(s) to {result['s3_uri']}"
        )
        formatter.print_info(
            f"Use --model-source {result['s3_uri']} when deploying inference endpoints"
        )

        if config.output_format != "table":
            formatter.print(result)

    except Exception as e:
        formatter.print_error(f"Failed to upload model: {e}")
        sys.exit(1)


@models.command("upload-regional")
@click.argument("local_path")
@click.option(
    "--region",
    "-r",
    required=True,
    help="Target region whose regional bucket receives the objects",
)
@click.option(
    "--prefix",
    default="uploads",
    show_default=True,
    help="S3 prefix for uploaded objects",
)
@pass_config
def models_upload_regional(config: Any, local_path: Any, region: Any, prefix: Any) -> None:
    """Upload local files or a directory to a region's regional bucket.

    Objects are written to that region's general-purpose
    gco-regional-shared-<account>-<region> bucket, resolved from the target
    region's own SSM parameter. The bucket is general purpose and usable by
    any in-region workload.

    Examples:
        gco models upload-regional ./data/ --region us-east-1
        gco models upload-regional ./file.bin -r eu-west-1 --prefix datasets
    """
    from ..models import get_regional_bucket_manager

    formatter = get_output_formatter(config)

    try:
        manager = get_regional_bucket_manager(config)
        formatter.print_info(f"Uploading {local_path} to region '{region}'...")
        result = manager.upload(local_path, region, prefix=prefix)

        formatter.print_success(
            f"Uploaded {result['files_uploaded']} file(s) to {result['s3_uri']}"
        )

        if config.output_format != "table":
            formatter.print(result)

    except Exception as e:
        formatter.print_error(f"Failed to upload to regional bucket: {e}")
        sys.exit(1)


@models.command("list")
@pass_config
def models_list(config: Any) -> None:
    """List models in the central S3 bucket.

    Examples:
        gco models list
    """
    from ..models import get_model_manager

    formatter = get_output_formatter(config)

    try:
        manager = get_model_manager(config)
        model_list = manager.list_models()

        if config.output_format != "table":
            formatter.print(model_list)
            return

        if not model_list:
            formatter.print_info("No models found. Upload with 'gco models upload'")
            return

        print(f"\n  Models ({len(model_list)} found)")
        print("  " + "-" * 70)
        print(f"  {'NAME':<25} {'FILES':>5} {'SIZE (GB)':>10} {'S3 URI'}")
        print("  " + "-" * 70)
        for m in model_list:
            print(
                f"  {m['model_name']:<25} {m['files']:>5} {m['total_size_gb']:>10.2f} {m['s3_uri']}"
            )
        print()

    except Exception as e:
        formatter.print_error(f"Failed to list models: {e}")
        sys.exit(1)


@models.command("delete")
@click.argument("model_name")
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation")
@pass_config
def models_delete(config: Any, model_name: Any, yes: Any) -> None:
    """Permanently delete a model and all object history from the central S3 bucket.

    Every current object, historical version, and delete marker beneath the
    model prefix is removed. Successful batches cannot be restored if a later
    batch fails.

    Examples:
        gco models delete llama3-8b -y
    """
    from ..models import get_model_manager

    formatter = get_output_formatter(config)

    if not yes:
        click.confirm(
            f"Permanently delete model '{model_name}', including all current files "
            "and historical S3 versions? This cannot be undone.",
            abort=True,
        )

    try:
        manager = get_model_manager(config)
        deleted = manager.delete_model(model_name)

        if deleted > 0:
            formatter.print_success(f"Deleted {deleted} file(s) for model '{model_name}'")
        else:
            formatter.print_warning(f"No files found for model '{model_name}'")

    except Exception as e:
        formatter.print_error(f"Failed to delete model: {e}")
        sys.exit(1)


@models.command("uri")
@click.argument("model_name")
@pass_config
def models_uri(config: Any, model_name: Any) -> None:
    """Get the S3 URI for a model (for use with --model-source).

    Examples:
        gco models uri llama3-8b
    """
    from ..models import get_model_manager

    formatter = get_output_formatter(config)

    try:
        manager = get_model_manager(config)
        uri = manager.get_model_uri(model_name)
        print(uri)

    except Exception as e:
        formatter.print_error(f"Failed to get model URI: {e}")
        sys.exit(1)
