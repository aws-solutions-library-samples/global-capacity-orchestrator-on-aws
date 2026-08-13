"""Fleet-wide status command for GCO."""

from __future__ import annotations

import click

from ..config import GCOConfig
from ..output import get_output_formatter
from ..status import FleetStatus, gather_fleet_status

pass_config = click.make_pass_decorator(GCOConfig, ensure=True)


def _render_table(doc: FleetStatus) -> None:
    """Render the document for a terminal reader."""
    print(f"Fleet status: {doc.overall}  (generated {doc.generated_at})")


@click.command("status")
@click.option("--region", "-r", help="Restrict the gather to a single region")
@pass_config
def status(config: GCOConfig, region: str | None) -> None:
    """Show fleet-wide deployment status across configured regions.

    Aggregates control-plane state — stacks, queue depth, jobs, capacity,
    and inference endpoints — into one document. Every section carries its
    own status, so a failed read degrades that section instead of hiding
    the rest.

    Examples:
        gco status
        gco status -r us-east-1
        gco status --output json
    """
    formatter = get_output_formatter(config)
    doc = gather_fleet_status(config, region=region)

    if config.output_format == "table":
        _render_table(doc)
    else:
        formatter.print(doc)
