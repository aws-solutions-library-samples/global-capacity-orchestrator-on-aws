"""
GCO CLI - Main entry point.

A comprehensive CLI for managing GCO multi-region EKS clusters.

Commands:
    gco stacks deploy-all -y                    # Deploy all infrastructure
    gco jobs submit-sqs job.yaml -r us-east-1   # Submit job via SQS (recommended)
    gco jobs submit job.yaml -n gco-jobs        # Submit job via API Gateway
    gco jobs list --all-regions                 # List jobs across regions
    gco capacity check -t g4dn.xlarge           # Check GPU capacity
    gco inference deploy my-llm -i ...          # Deploy inference endpoint
    gco stacks destroy-all -y                   # Tear down everything

Full reference: docs/CLI.md
"""

import logging
import os

import click

from . import __version__
from .commands import (
    analytics,
    autopilot,
    capacity,
    cluster,
    config_cmd,
    costs,
    dag,
    examples,
    files,
    images,
    inference,
    jobs,
    mission_cmd,
    models,
    monitoring,
    nodepools,
    queue,
    release,
    stacks,
    storage,
    tasks,
    templates,
    webhooks,
)
from .config import get_config


def _configure_cli_logging(verbose: bool) -> None:
    """
    Configure logging for the CLI.

    By default, the CLI is quiet: only WARNING and above from our own code,
    and the chatty AWS SDK / HTTP stack loggers (``botocore``, ``boto3``,
    ``urllib3``, ``s3transfer``, ``kubernetes``) are pinned at WARNING so
    credential-discovery INFO messages and retry-attempt INFO messages don't
    clutter normal output.

    ``--verbose`` / ``-v`` (or ``GCO_LOG_LEVEL=DEBUG``) turns on DEBUG for
    everything, which is the right escape hatch when something is actually
    wrong and you need to see what the SDK is doing.

    This function also calls ``logging.basicConfig`` with ``force=True`` so
    it overrides any ``basicConfig`` that might have been called at import
    time by a library module (the CLI owns its log configuration).
    """
    env_level = os.environ.get("GCO_LOG_LEVEL")
    if verbose or (env_level and env_level.upper() == "DEBUG"):
        level = logging.DEBUG
    elif env_level:
        level = getattr(logging, env_level.upper(), logging.WARNING)
    else:
        level = logging.WARNING

    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        force=True,
    )

    # Pin noisy third-party loggers even when we're at DEBUG, unless the
    # user explicitly asked for verbose output. This keeps ``-v`` useful
    # for seeing OUR logs without being drowned by boto's retry chatter.
    third_party_level = logging.DEBUG if verbose else logging.WARNING
    for name in ("botocore", "boto3", "urllib3", "s3transfer", "kubernetes"):
        logging.getLogger(name).setLevel(third_party_level)


@click.group()
@click.version_option(version=__version__, prog_name="gco")
@click.option("--config", "-c", "config_file", help="Path to config file")
@click.option("--region", "-r", "default_region", help="Default AWS region")
@click.option(
    "--output",
    "-o",
    "output_format",
    type=click.Choice(["table", "json", "yaml"]),
    default=None,
    help="Output format (defaults to the configured value)",
)
@click.option("--verbose", "-v", is_flag=True, default=None, help="Verbose output")
@click.option(
    "--regional-api/--global-api",
    default=None,
    help="Use regional API endpoints, or explicitly use the global endpoint",
)
@click.pass_context
def cli(
    ctx: click.Context,
    config_file: str | None,
    default_region: str | None,
    output_format: str | None,
    verbose: bool | None,
    regional_api: bool | None,
) -> None:
    """GCO CLI - Manage multi-region EKS clusters for AI/ML workloads."""
    config = get_config(config_file)

    if default_region:
        config.default_region = default_region
    if output_format:
        config.output_format = output_format
    if verbose is not None:
        config.verbose = verbose
    if regional_api is not None:
        config.use_regional_api = regional_api

    _configure_cli_logging(config.verbose)
    ctx.obj = config


# Register command groups
cli.add_command(autopilot)
cli.add_command(jobs)
cli.add_command(dag)
cli.add_command(queue)
cli.add_command(release)
cli.add_command(examples)
cli.add_command(templates)
cli.add_command(webhooks)
cli.add_command(capacity)
cli.add_command(cluster)
cli.add_command(inference)
cli.add_command(images)
cli.add_command(models)
cli.add_command(nodepools)
cli.add_command(costs)
cli.add_command(stacks)
cli.add_command(storage)
cli.add_command(files)
cli.add_command(config_cmd)
cli.add_command(analytics)
cli.add_command(monitoring)
cli.add_command(tasks)
cli.add_command(mission_cmd)


def main() -> None:
    """Main entry point for the CLI."""
    cli(obj=None)


if __name__ == "__main__":
    main()
