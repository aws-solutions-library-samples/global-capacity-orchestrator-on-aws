"""Webhook commands."""

import sys
from typing import Any

import click

from ..config import GCOConfig
from ..output import get_output_formatter

pass_config = click.make_pass_decorator(GCOConfig, ensure=True)


@click.group()
@pass_config
def webhooks(config: Any) -> None:
    """Manage webhooks for job event notifications.

    Webhooks receive HTTP POST notifications when job events occur
    (job.started, job.completed, job.failed).
    """
    pass


@webhooks.command("list")
@click.option("--namespace", "-n", help="Filter by namespace")
@click.option("--region", "-r", help="Region to query (any region works)")
@pass_config
def webhooks_list(config: Any, namespace: Any, region: Any) -> None:
    """List all registered webhooks.

    Examples:
        gco webhooks list
        gco webhooks list --namespace gco-jobs
    """
    formatter = get_output_formatter(config)

    try:
        from ..aws_client import get_aws_client

        aws_client = get_aws_client(config)

        query_region = region or config.default_region
        params = {}
        if namespace:
            params["namespace"] = namespace

        result = aws_client.call_api(
            method="GET",
            path="/api/v1/webhooks",
            region=query_region,
            params=params,
        )

        if config.output_format == "table":
            webhooks_data = result.get("webhooks", [])
            if not webhooks_data:
                formatter.print_info("No webhooks found")
                return

            print(f"\n  Webhooks ({result.get('count', 0)} total)")
            print("  " + "-" * 80)
            print(
                "  ID        URL                                      EVENTS              NAMESPACE"
            )
            print("  " + "-" * 80)
            for w in webhooks_data:
                wid = w.get("id", "")[:8]
                url = w.get("url", "")[:40]
                events = ",".join(w.get("events", []))[:18]
                ns = (w.get("namespace") or "all")[:12]
                print(f"  {wid:<9} {url:<42} {events:<19} {ns}")
        else:
            formatter.print(result)

    except Exception as e:
        formatter.print_error(f"Failed to list webhooks: {e}")
        sys.exit(1)


@webhooks.command("get")
@click.argument("webhook_id")
@click.option("--region", "-r", help="Region to query (any region works)")
@pass_config
def webhooks_get(config: Any, webhook_id: Any, region: Any) -> None:
    """Get a single webhook by id.

    The webhooks API exposes list/create/delete but no fetch-by-id endpoint, so
    this lists the region's webhooks and returns the one whose id matches.

    Examples:
        gco webhooks get abc12345
        gco webhooks get abc12345 -r us-east-1
    """
    formatter = get_output_formatter(config)

    try:
        from ..aws_client import get_aws_client

        aws_client = get_aws_client(config)

        query_region = region or config.default_region
        result = aws_client.call_api(
            method="GET",
            path="/api/v1/webhooks",
            region=query_region,
        )
        match = next(
            (w for w in result.get("webhooks", []) if w.get("id") == webhook_id),
            None,
        )
        if match is None:
            formatter.print_error(f"Webhook '{webhook_id}' not found")
            sys.exit(1)
        formatter.print(match)

    except SystemExit:
        raise
    except Exception as e:
        formatter.print_error(f"Failed to get webhook: {e}")
        sys.exit(1)


@webhooks.command("create")
@click.option("--url", "-u", required=True, help="Webhook URL")
@click.option(
    "--event",
    "-e",
    multiple=True,
    required=True,
    type=click.Choice(["job.started", "job.completed", "job.failed"]),
    help="Events to subscribe to",
)
@click.option("--namespace", "-n", help="Filter events by namespace")
@click.option("--secret", "-s", help="Secret for HMAC signature verification")
@click.option("--region", "-r", help="Region to use (any region works)")
@pass_config
def webhooks_create(
    config: Any, url: Any, event: Any, namespace: Any, secret: Any, region: Any
) -> None:
    """Register a new webhook for job events.

    Examples:
        gco webhooks create --url https://example.com/webhook -e job.completed -e job.failed
        gco webhooks create -u https://slack.com/webhook -e job.failed -n gco-jobs
    """
    formatter = get_output_formatter(config)

    try:
        from ..aws_client import get_aws_client

        aws_client = get_aws_client(config)

        query_region = region or config.default_region
        result = aws_client.call_api(
            method="POST",
            path="/api/v1/webhooks",
            region=query_region,
            body={
                "url": url,
                "events": list(event),
                "namespace": namespace,
                "secret": secret,
            },
        )

        formatter.print_success("Webhook registered successfully")
        formatter.print(result)

    except Exception as e:
        formatter.print_error(f"Failed to create webhook: {e}")
        sys.exit(1)


@webhooks.command("delete")
@click.argument("webhook_id")
@click.option("--region", "-r", help="Region to use (any region works)")
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation")
@pass_config
def webhooks_delete(config: Any, webhook_id: Any, region: Any, yes: Any) -> None:
    """Delete a webhook.

    Examples:
        gco webhooks delete abc123
        gco webhooks delete abc123 -y
    """
    formatter = get_output_formatter(config)

    if not yes:
        click.confirm(f"Delete webhook '{webhook_id}'?", abort=True)

    try:
        from ..aws_client import get_aws_client

        aws_client = get_aws_client(config)

        query_region = region or config.default_region
        result = aws_client.call_api(
            method="DELETE",
            path=f"/api/v1/webhooks/{webhook_id}",
            region=query_region,
        )

        formatter.print_success(f"Webhook '{webhook_id}' deleted")
        formatter.print(result)

    except Exception as e:
        formatter.print_error(f"Failed to delete webhook: {e}")
        sys.exit(1)
