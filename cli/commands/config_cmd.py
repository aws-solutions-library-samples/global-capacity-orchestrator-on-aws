"""Configuration commands."""

import sys
from typing import Any

import click

from ..config import MACHINE_CONFIG_KEYS, GCOConfig, environment_settings
from ..output import confirm, get_output_formatter

pass_config = click.make_pass_decorator(GCOConfig, ensure=True)


@click.group(name="config-cmd")
@pass_config
def config_cmd(config: Any) -> None:
    """Manage CLI configuration."""


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


#: Root options ``config-cmd init`` records, by the setting each one sets.
_ROOT_OPTION_SETTINGS = {
    "default_region": "default_region",
    "output_format": "output_format",
    "verbose": "verbose",
    "regional_api": "use_regional_api",
}


def _settings_given_to_this_invocation() -> set[str]:
    """Per-machine settings this command was given: root options and ``GCO_*`` variables."""
    given = environment_settings()
    root = click.get_current_context().find_root()
    given.update(
        setting
        for option, setting in _ROOT_OPTION_SETTINGS.items()
        if root.params.get(option) is not None
    )
    return given & set(MACHINE_CONFIG_KEYS)


@config_cmd.command("init")
@click.option("--force", "-f", is_flag=True, help="Overwrite existing config")
@pass_config
def init_config(config: Any, force: Any) -> None:
    """Write a starter ~/.gco/config.yaml.

    Settings given to this command (the root -r, -o, -v and
    --regional-api/--global-api options, or GCO_* environment variables) are
    written as values; the other per-machine settings are listed as
    commented-out defaults. project_name and the Regions are never written:
    each checkout's cdk.json supplies them, and a value in this file would
    override the cdk.json of every checkout.
    """
    from pathlib import Path

    config_path = Path.home() / ".gco" / "config.yaml"

    if config_path.exists() and not force:
        confirm(f"Config file exists at {config_path}. Overwrite?", abort=True)

    config.save(str(config_path), keys=_settings_given_to_this_invocation())
    click.echo(f"Configuration saved to {config_path}")
