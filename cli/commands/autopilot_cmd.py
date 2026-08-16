"""Autopilot command: one command from a plain terminal to a working agent."""

import json
import sys
from pathlib import Path
from typing import Any

import click

from ..autopilot import (
    CLAUDE_CODE_PACKAGE,
    CLAUDE_CODE_VERSION,
    build_claude_env,
    build_launch_argv,
    build_mcp_config,
    build_plugin_args,
    claude_install_command,
    config_path,
    exec_claude,
    find_claude_binary,
    has_resumable_session,
    install_claude_code,
    resolve_mcp_flags,
    resolve_model,
    resolve_plugin_paths,
    resolve_small_fast_model,
    stage_imports,
    validate_imports,
    write_mcp_config,
)
from ..config import GCOConfig
from ..output import get_output_formatter

#: claude's own session-resumption flags. When one of these appears in the
#: passthrough args (after ``--``), the caller has already made a resume
#: choice and autopilot neither prompts nor injects its own flags.
_CLAUDE_RESUME_FLAGS = frozenset({"-c", "--continue", "-r", "--resume"})

pass_config = click.make_pass_decorator(GCOConfig, ensure=True)


def _stdin_is_interactive() -> bool:
    """Whether a human is on the other end (gates the resume prompt)."""
    return sys.stdin.isatty()


def _parse_mcp_env(pairs: tuple[str, ...]) -> dict[str, str]:
    """Parse ``--mcp-env KEY=VALUE`` pairs, rejecting malformed input."""
    env: dict[str, str] = {}
    for pair in pairs:
        key, separator, value = pair.partition("=")
        if not separator or not key.strip():
            raise ValueError(f"--mcp-env expects KEY=VALUE, got {pair!r}.")
        env[key.strip()] = value
    return env


def _plan(
    config: Any,
    model: str | None,
    small_fast_model: str | None,
    companions: bool,
    enable: tuple[str, ...] = (),
    mcp_env: tuple[str, ...] = (),
    plugins: tuple[str, ...] = (),
    skills: tuple[str, ...] = (),
    agents: tuple[str, ...] = (),
) -> tuple[dict[str, Any], list[str]]:
    """Resolve everything a launch needs, without side effects.

    Import sources are validated here (so ``--dry-run`` catches typos) but
    staged only at launch — a dry run writes nothing.
    """
    resolved_model, warnings = resolve_model(model)
    resolved_small = resolve_small_fast_model(small_fast_model)
    # Flags first, generic env second: an explicit KEY=VALUE wins over the
    # "true" a --enable of the same flag would set.
    gco_mcp_env = resolve_mcp_flags(enable)
    gco_mcp_env.update(_parse_mcp_env(mcp_env))
    plugin_paths = resolve_plugin_paths(plugins)
    validate_imports(skills, agents)
    workspace = Path.cwd()
    mcp_config = build_mcp_config(workspace, include_companions=companions, gco_mcp_env=gco_mcp_env)
    claude_binary = find_claude_binary()
    plan = {
        "model": resolved_model,
        "small_fast_model": resolved_small,
        "region": config.default_region,
        "workspace": str(workspace),
        "mcp_config_path": str(config_path()),
        "mcp_servers": sorted(mcp_config["mcpServers"]),
        "gco_mcp_env": dict(sorted(gco_mcp_env.items())),
        "plugins": [str(path) for path in plugin_paths],
        "import_skills": [str(Path(item).expanduser()) for item in skills],
        "import_agents": [str(Path(item).expanduser()) for item in agents],
        "claude_binary": claude_binary,
        "claude_code_pin": f"{CLAUDE_CODE_PACKAGE}@{CLAUDE_CODE_VERSION}",
        "install_command": " ".join(claude_install_command()),
        "resumable_session": has_resumable_session(workspace),
        "mcp_config": mcp_config,
    }
    return plan, warnings


def _resolve_resume_args(
    plan: dict[str, Any],
    continue_session: bool,
    resume: str | None,
    claude_args: tuple[str, ...],
    yes: bool,
) -> tuple[str, ...]:
    """Decide which claude resume flags this launch carries.

    Explicit wins: ``--continue`` / ``--resume`` (ours, or claude's own in
    the passthrough args) are honored as given. Otherwise, when Claude Code
    already has a session for this workspace and we're on an interactive
    terminal, offer to pick it up — one keypress instead of retyping
    context. The prompt is skipped for ``--yes`` and non-TTY runs so
    scripted invocations never hang, and a fresh session stays the default.
    """
    if continue_session:
        return ("--continue",)
    if resume is not None:
        return ("--resume",) if resume == "" else ("--resume", resume)
    if _CLAUDE_RESUME_FLAGS & set(claude_args):
        return ()
    if (
        not yes
        and plan["resumable_session"]
        and _stdin_is_interactive()
        and click.confirm(
            "Resume your previous Claude Code session in this workspace?",
            default=False,
        )
    ):
        return ("--continue",)
    return ()


def _print_dry_run(formatter: Any, plan: dict[str, Any]) -> None:
    """Render the launch plan as the table-format summary."""
    print()
    print("  GCO Autopilot — launch plan")
    print("  " + "-" * 68)
    print(f"  Model (Bedrock):   {plan['model']}")
    if plan["small_fast_model"]:
        print(f"  Fast model:        {plan['small_fast_model']}")
    print(f"  AWS region:        {plan['region']}")
    print(f"  Workspace:         {plan['workspace']}")
    print(f"  MCP config:        {plan['mcp_config_path']}  (--strict-mcp-config)")
    print(f"  MCP servers ({len(plan['mcp_servers'])}):   " + ", ".join(plan["mcp_servers"]))
    if plan["gco_mcp_env"]:
        rendered = ", ".join(f"{k}={v}" for k, v in plan["gco_mcp_env"].items())
        print(f"  GCO MCP env:       {rendered}")
    else:
        print("  GCO MCP env:       (none — default read-only toolset)")
    if plan["plugins"]:
        print(f"  Plugins:           {', '.join(plan['plugins'])}")
    imports = [f"skills:{path}" for path in plan["import_skills"]] + [
        f"agents:{path}" for path in plan["import_agents"]
    ]
    if imports:
        print(f"  Imports:           {', '.join(imports)}  (staged as a session plugin)")
    if plan["claude_binary"]:
        print(f"  Claude Code:       {plan['claude_binary']}")
    else:
        print(f"  Claude Code:       not installed — will offer: {plan['install_command']}")
    if plan["resumable_session"]:
        print("  Previous session:  found — launch will offer to resume (or pass --continue)")
    else:
        print("  Previous session:  none for this workspace")
    print("  " + "-" * 68)
    print("  Dry run only — nothing was written or launched.")
    print()


@click.command("autopilot")
@click.option(
    "--model",
    "-m",
    default=None,
    help=(
        "Bedrock model or inference-profile id for the session "
        "(default: cdk.json context.bedrock.claude_code_default_model_id; "
        "env override: GCO_AUTOPILOT_MODEL)."
    ),
)
@click.option(
    "--small-fast-model",
    default=None,
    help=(
        "Optional Bedrock model for Claude Code's background/fast tasks "
        "(env override: GCO_AUTOPILOT_SMALL_FAST_MODEL; unset by default)."
    ),
)
@click.option(
    "--companions/--no-companions",
    "companions",
    default=True,
    help="Include the recommended companion MCP servers (default: yes).",
)
@click.option(
    "--enable",
    "-e",
    "enable",
    multiple=True,
    metavar="FLAG",
    help=(
        "Enable a GCO MCP feature flag for the session (repeatable). "
        "Accepts the short form (mission, all-tools, infrastructure-deploy) "
        "or the full GCO_ENABLE_* name; unknown flags fail with the valid list."
    ),
)
@click.option(
    "--mcp-env",
    "mcp_env",
    multiple=True,
    metavar="KEY=VALUE",
    help=(
        "Set an arbitrary environment variable on the GCO MCP server "
        "(repeatable), e.g. GCO_MCP_TOOL_SEARCH=bm25. Wins over --enable "
        "for the same key."
    ),
)
@click.option(
    "--plugin",
    "plugins",
    multiple=True,
    metavar="PATH",
    help=(
        "Load a Claude Code plugin directory or .zip into the session "
        "(repeatable; env: GCO_AUTOPILOT_PLUGIN_DIRS, colon-separated). "
        "Plugins can bundle skills, agents, commands, and hooks."
    ),
)
@click.option(
    "--skills",
    "skills",
    multiple=True,
    metavar="DIR",
    help=(
        "Import a directory of skills (one subdirectory per skill, each "
        "with a SKILL.md) into the session (repeatable). Staged as a "
        "session-scoped plugin — nothing is copied into your project or "
        "~/.claude."
    ),
)
@click.option(
    "--agents",
    "agents",
    multiple=True,
    metavar="DIR",
    help=(
        "Import a directory of agent files (*.md subagent definitions) "
        "into the session (repeatable). Staged like --skills."
    ),
)
@click.option(
    "--continue",
    "-c",
    "continue_session",
    is_flag=True,
    help="Resume the most recent Claude Code session for this workspace.",
)
@click.option(
    "--resume",
    "resume",
    is_flag=False,
    flag_value="",
    default=None,
    metavar="[SESSION_ID]",
    help=(
        "Resume a specific Claude Code session by id, or open claude's "
        "interactive session picker when no id is given."
    ),
)
@click.option(
    "--dry-run",
    is_flag=True,
    help="Show the resolved launch plan without installing, writing, or launching.",
)
@click.option(
    "--print-config",
    "print_config",
    is_flag=True,
    help="Print the generated MCP config JSON to stdout and exit.",
)
@click.option("--yes", "-y", is_flag=True, help="Install Claude Code without prompting if absent.")
@click.argument("claude_args", nargs=-1, type=click.UNPROCESSED)
@pass_config
def autopilot(
    config: Any,
    model: Any,
    small_fast_model: Any,
    companions: Any,
    enable: Any,
    mcp_env: Any,
    plugins: Any,
    skills: Any,
    agents: Any,
    continue_session: Any,
    resume: Any,
    dry_run: Any,
    print_config: Any,
    yes: Any,
    claude_args: Any,
) -> None:
    """Launch a fully configured Claude Code session for GCO.

    One command turns this terminal into an agent session with the GCO MCP
    server plus the recommended companion MCP servers, talking to Amazon
    Bedrock with your AWS credentials. The model defaults to GCO's Claude
    Code default (cdk.json context.bedrock.claude_code_default_model_id)
    and can be any Claude model or inference profile enabled on Bedrock.

    If the Claude Code CLI is not installed, autopilot offers to install
    the exact pinned release via npm — nothing is baked into the dev
    container, so setup happens on first use.

    Arguments after ``--`` are passed through to the claude CLI.

    When Claude Code already has a session for this workspace, launching
    interactively offers to resume it; ``--continue`` / ``--resume`` skip
    the prompt and resume directly.

    GCO MCP feature flags gate the server's opt-in tool groups (deploys,
    destroys, capacity purchases, Mission, ...). By default the session
    gets the read-only toolset; pass --enable per flag, or --enable
    all-tools for everything. --mcp-env sets any other server variable.

    Bring your own context: --plugin loads Claude Code plugin dirs/zips
    for this session, and --skills / --agents import loose directories of
    skills or subagent files without touching your project or ~/.claude.

    \b
    Examples:
        gco autopilot
        gco autopilot --continue
        gco autopilot --resume
        gco autopilot -e mission -e infrastructure-deploy
        gco autopilot -e all-tools
        gco autopilot --mcp-env GCO_MCP_TOOL_SEARCH=bm25
        gco autopilot --skills ~/team-skills --agents ~/my-agents
        gco autopilot --plugin ~/plugins/incident-response
        gco autopilot -m global.anthropic.claude-sonnet-4-6
        gco autopilot --dry-run
        gco autopilot --print-config
        gco autopilot -y -- --permission-mode plan

    \b
    Requirements:
        - AWS credentials with bedrock:InvokeModel for the chosen model
        - The model enabled in your account (Anthropic first-time-use form)
        - npm (only when Claude Code is not already installed)
    """
    formatter = get_output_formatter(config)

    if continue_session and resume is not None:
        formatter.print_error("Pass either --continue or --resume, not both.")
        sys.exit(1)

    try:
        plan, warnings = _plan(
            config,
            model,
            small_fast_model,
            companions,
            tuple(enable),
            tuple(mcp_env),
            tuple(plugins),
            tuple(skills),
            tuple(agents),
        )
    except ValueError as e:
        formatter.print_error(str(e))
        sys.exit(1)
    except Exception as e:
        formatter.print_error(f"Failed to resolve the autopilot launch plan: {e}")
        sys.exit(1)

    for warning in warnings:
        formatter.print_warning(warning)

    if print_config:
        # Always raw JSON, regardless of --output: this is the
        # machine-readable surface CI and scripts consume.
        click.echo(json.dumps(plan["mcp_config"], indent=2))
        return

    if dry_run:
        if config.output_format == "table":
            _print_dry_run(formatter, plan)
        else:
            formatter.print({k: v for k, v in plan.items() if k != "mcp_config"})
        return

    claude_binary = plan["claude_binary"]
    if claude_binary is None:
        formatter.print_info(f"Claude Code is not installed (pinned: {plan['claude_code_pin']}).")
        if not yes and not click.confirm(f"Install it now with `{plan['install_command']}`?"):
            formatter.print_error(
                "Claude Code is required. Install it manually with "
                f"`{plan['install_command']}` and re-run `gco autopilot`."
            )
            sys.exit(1)
        rc = install_claude_code()
        if rc == 127:
            formatter.print_error(
                "npm was not found on PATH. Install Node.js/npm (the GCO dev "
                "container ships both), or install Claude Code another way, "
                "then re-run `gco autopilot`."
            )
            sys.exit(1)
        if rc != 0:
            formatter.print_error(f"`{plan['install_command']}` failed with exit code {rc}.")
            sys.exit(1)
        claude_binary = find_claude_binary()
        if claude_binary is None:
            formatter.print_error(
                "Claude Code installed but the `claude` binary is not on PATH. "
                "Open a new shell (or fix your npm global bin path) and re-run."
            )
            sys.exit(1)

    try:
        written = write_mcp_config(plan["mcp_config"])
        staged_plugin = stage_imports(tuple(skills), tuple(agents))
    except (OSError, ValueError) as e:
        formatter.print_error(f"Failed to prepare the session: {e}")
        sys.exit(1)

    plugin_paths = [Path(path) for path in plan["plugins"]]
    if staged_plugin is not None:
        plugin_paths.append(staged_plugin)
    plugin_args = build_plugin_args(plugin_paths)

    resume_args = _resolve_resume_args(plan, continue_session, resume, tuple(claude_args), yes)

    env = build_claude_env(plan["model"], plan["region"], plan["small_fast_model"])
    argv = build_launch_argv(claude_binary, written, tuple(claude_args), resume_args, plugin_args)

    formatter.print_info(f"Launching Claude Code on Bedrock ({plan['model']})...")
    try:
        rc = exec_claude(argv, env)  # returns only on Windows
    except OSError as e:
        # A claude on PATH that cannot exec (for example a shim whose
        # blocked postinstall never fetched the native binary) must fail
        # with a remediation, not a traceback.
        formatter.print_error(
            f"Failed to launch Claude Code at {argv[0]}: {e}. "
            "The install may be incomplete — reinstall with "
            f"`{' '.join(claude_install_command())}` and re-run `gco autopilot`."
        )
        sys.exit(1)
    sys.exit(rc)
