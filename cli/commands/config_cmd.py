"""Configuration commands."""

import sys
from typing import Any

import click

from ..config import GCOConfig
from ..output import get_output_formatter

pass_config = click.make_pass_decorator(GCOConfig, ensure=True)


@click.group(name="config-cmd")
@pass_config
def config_cmd(config: Any) -> None:
    """Manage CLI configuration."""
    pass


@config_cmd.command("show")
@pass_config
def show_config(config: Any) -> None:
    """Show current configuration."""
    formatter = get_output_formatter(config)
    formatter.print(config.to_dict())


@config_cmd.command("get")
@click.argument("key", required=False)
@pass_config
def get_config(config: Any, key: Any) -> None:
    """Read a configuration value by KEY (dotted path), or the full config.

    Examples:
        gco config-cmd get
        gco config-cmd get default_region
    """
    formatter = get_output_formatter(config)
    data = config.to_dict()
    if not key:
        formatter.print(data)
        return

    node: Any = data
    for part in key.split("."):
        if isinstance(node, dict) and part in node:
            node = node[part]
        else:
            formatter.print_error(f"Config key not found: {key}")
            sys.exit(1)
    formatter.print({key: node})


@config_cmd.command("init")
@click.option("--force", "-f", is_flag=True, help="Overwrite existing config")
@pass_config
def init_config(config: Any, force: Any) -> None:
    """Initialize configuration file."""
    from pathlib import Path

    config_path = Path.home() / ".gco" / "config.yaml"

    if config_path.exists() and not force:
        click.confirm(f"Config file exists at {config_path}. Overwrite?", abort=True)

    config.save(str(config_path))
    click.echo(f"Configuration saved to {config_path}")
