"""
GCO CLI - Main entry point.

A comprehensive CLI for managing GCO multi-region EKS clusters.

Commands:
    gco stacks deploy-all -y                    # Deploy all infrastructure
    gco jobs submit-sqs job.yaml -r us-east-1   # Submit job via SQS (recommended)
    gco jobs submit job.yaml -n gco-jobs        # Submit job via API Gateway
    gco jobs list --all-regions                 # List jobs across regions
    gco capacity check -i g4dn.xlarge           # Check GPU capacity
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
    crossplane,
    dag,
    deps,
    examples,
    files,
    gitops,
    images,
    inference,
    jobs,
    mission_cmd,
    models,
    monitoring,
    nodepools,
    queue,
    release,
    slurm,
    stacks,
    status,
    storage,
    swarm_cmd,
    tasks,
    templates,
    upgrade,
    vector,
    webhooks,
)
from .config import get_config
from .log_redaction import CredentialRedactionFilter
from .output import StructuredOutputGroup

#: The AWS SDK and HTTP-stack loggers the CLI pins. At DEBUG they print
#: material a user must never paste anywhere: the SigV4 CanonicalRequest with
#: the session token, the Authorization signature, and (botocore.parsers)
#: every response body, STS credentials included. ``-v`` therefore stops at
#: INFO for them; DEBUG is the explicit ``--debug-aws`` (see cli.log_redaction).
_SDK_LOGGERS = ("botocore", "boto3", "urllib3", "s3transfer")
#: The Kubernetes client logs response bodies (Secret data included) at DEBUG
#: and is never raised past INFO.
_KUBERNETES_LOGGER = "kubernetes"
#: botocore's response parser logs every response body at DEBUG; it stays at
#: INFO even under ``--debug-aws``.
_RESPONSE_BODY_LOGGER = "botocore.parsers"


def _configure_cli_logging(verbose: bool, debug_aws: bool = False) -> None:
    """
    Configure logging for the CLI.

    By default, the CLI is quiet: only WARNING and above from our own code,
    and the chatty AWS SDK / HTTP stack loggers (``botocore``, ``boto3``,
    ``urllib3``, ``s3transfer``, ``kubernetes``) are pinned at WARNING so
    credential-discovery INFO messages and retry-attempt INFO messages don't
    clutter normal output.

    ``--verbose`` / ``-v`` (or ``GCO_LOG_LEVEL=DEBUG``) turns on DEBUG for
    GCO's own loggers. ``-v`` also lets the SDK loggers speak at INFO (which
    credential provider was used, connection retries) but never DEBUG: SDK
    debug output carries signing material and credentials, so it is the
    separate, flag-only ``--debug-aws``, which raises the SDK loggers to
    DEBUG while keeping ``botocore.parsers`` (response bodies) at INFO. In
    every mode the root handler carries a ``CredentialRedactionFilter``.

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
    redaction = CredentialRedactionFilter()
    for handler in logging.getLogger().handlers:
        handler.addFilter(redaction)

    # Pin the third-party loggers even when we're at DEBUG. ``-v`` opens them
    # to INFO so the user sees which credentials were found and whether
    # requests are being retried, without boto's per-request debug chatter
    # (and without the credentials inside it).
    third_party_level = logging.INFO if verbose else logging.WARNING
    for name in (*_SDK_LOGGERS, _KUBERNETES_LOGGER):
        logging.getLogger(name).setLevel(third_party_level)
    if debug_aws:
        for name in _SDK_LOGGERS:
            logging.getLogger(name).setLevel(logging.DEBUG)
    # Response bodies are never logged: sts:AssumeRole answers with a full
    # credential set, secretsmanager:GetSecretValue with the secret.
    logging.getLogger(_RESPONSE_BODY_LOGGER).setLevel(logging.INFO if debug_aws else logging.NOTSET)


#: Printed to stderr whenever ``--debug-aws`` is on, before any SDK call.
_DEBUG_AWS_NOTICE = (
    "AWS SDK debug logging is on: request parameters and SigV4 signing details go to "
    "stderr. Signatures, session tokens and credential fields are redacted and response "
    "bodies are never logged, but treat this output as sensitive."
)


@click.group(cls=StructuredOutputGroup)
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
@click.option(
    "--verbose",
    "-v",
    is_flag=True,
    default=None,
    help="Verbose output: GCO debug logging plus AWS SDK info messages",
)
@click.option(
    "--debug-aws",
    is_flag=True,
    default=False,
    help=(
        "Log AWS SDK requests and SigV4 signing details to stderr (credentials redacted, "
        "response bodies never logged). Flag only: no config or environment equivalent."
    ),
)
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
    debug_aws: bool,
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

    # debug_aws is deliberately not a config or environment setting: SDK debug
    # output is a per-invocation decision, and the MCP server's CLI runner
    # (which returns CLI stderr to its client) inherits the host environment.
    _configure_cli_logging(config.verbose, debug_aws=debug_aws)
    if debug_aws:
        click.echo(_DEBUG_AWS_NOTICE, err=True)
    ctx.obj = config


# Register command groups
cli.add_command(autopilot)
cli.add_command(jobs)
cli.add_command(dag)
cli.add_command(deps)
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
cli.add_command(gitops)
cli.add_command(crossplane)
cli.add_command(tasks)
cli.add_command(mission_cmd)
cli.add_command(swarm_cmd)
cli.add_command(vector)
cli.add_command(status)
cli.add_command(upgrade)
cli.add_command(slurm)


def main() -> None:
    """Main entry point for the CLI."""
    cli(obj=None)


if __name__ == "__main__":
    main()
