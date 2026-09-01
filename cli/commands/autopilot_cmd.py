"""Autopilot command: one command from a plain terminal to a working agent."""

import json
import sys
import tomllib
from pathlib import Path
from typing import Any

import click

from ..autopilot import (
    CLAUDE_CODE_PACKAGE,
    CLAUDE_CODE_VERSION,
    CODEX_BEDROCK_PROVIDER,
    CODEX_PACKAGE,
    CODEX_VERSION,
    AutopilotEngine,
    build_claude_env,
    build_codex_config_toml,
    build_codex_env,
    build_codex_launch_argv,
    build_codex_owned_args,
    build_launch_argv,
    build_mcp_config,
    build_plugin_args,
    claude_install_command,
    codex_config_path,
    codex_install_command,
    config_path,
    effective_aws_region,
    exec_claude,
    exec_codex,
    find_claude_binary,
    find_codex_binary,
    has_resumable_session,
    install_claude_code,
    install_codex,
    plugin_paths_requested,
    resolve_codex_model,
    resolve_codex_reasoning_effort,
    resolve_engine,
    resolve_mcp_flags,
    resolve_model,
    resolve_plugin_paths,
    resolve_small_fast_model,
    stage_codex_skills,
    stage_imports,
    validate_imports,
    write_codex_config,
    write_mcp_config,
)
from ..config import GCOConfig
from ..output import get_output_formatter

# <pyflowchart-code-diagram> BEGIN - auto-inserted, do not edit
# Generated at (UTC): 2026-09-01T13:22:56Z
# Generated from Git commit: ed395032d46063f44b638deb85ae2a6dbf98e7f4
# Flowchart(s) generated from this file:
#   * ``_plan`` -> ``diagrams/code_diagrams/cli/commands/autopilot_cmd._plan.html``
#     (PNG: ``diagrams/code_diagrams/cli/commands/autopilot_cmd._plan.png``)
# Regenerate with ``SOURCE_DATE_EPOCH=<unix-seconds> GCO_DIAGRAM_SOURCE_COMMIT=<40-char-sha> python diagrams/generate.py --code-only``.
# <pyflowchart-code-diagram> END


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
    engine: str | AutopilotEngine | None = None,
) -> tuple[dict[str, Any], list[str]]:
    """Resolve an engine launch plan without installing or writing files."""
    resolved_engine = resolve_engine(engine)
    gco_mcp_env = resolve_mcp_flags(enable)
    gco_mcp_env.update(_parse_mcp_env(mcp_env))
    workspace = Path.cwd()
    mcp_config = build_mcp_config(
        workspace,
        include_companions=companions,
        gco_mcp_env=gco_mcp_env,
    )
    resolved_region = effective_aws_region(config.default_region)

    if resolved_engine is AutopilotEngine.CODEX:
        resolved_small = resolve_small_fast_model(small_fast_model)
        if resolved_small is not None:
            raise ValueError(
                "--small-fast-model and GCO_AUTOPILOT_SMALL_FAST_MODEL are "
                "supported only by the claude-code engine"
            )
        if plugin_paths_requested(plugins):
            raise ValueError(
                "--plugin and GCO_AUTOPILOT_PLUGIN_DIRS are Claude Code plugin "
                "inputs and are not supported by the codex engine"
            )
        plugin_paths: list[Path] = []
        if agents:
            raise ValueError(
                "--agents imports Claude Code agent files and is not supported by the codex engine"
            )
        validate_imports(skills, ())
        resolved_model, warnings = resolve_codex_model(model)
        reasoning_effort = resolve_codex_reasoning_effort(model)
        codex_config = build_codex_config_toml(
            mcp_config,
            model=resolved_model,
            region=resolved_region,
            reasoning_effort=reasoning_effort,
        )
        binary = find_codex_binary()
        pin = f"{CODEX_PACKAGE}@{CODEX_VERSION}"
        install_command = codex_install_command()
        config_file = codex_config_path()
        display_name = "Codex"
        resumable = False
    else:
        resolved_model, warnings = resolve_model(model)
        resolved_small = resolve_small_fast_model(small_fast_model)
        plugin_paths = resolve_plugin_paths(plugins)
        validate_imports(skills, agents)
        reasoning_effort = None
        codex_config = None
        binary = find_claude_binary()
        pin = f"{CLAUDE_CODE_PACKAGE}@{CLAUDE_CODE_VERSION}"
        install_command = claude_install_command()
        config_file = config_path()
        display_name = "Claude Code"
        resumable = has_resumable_session(workspace)

    plan = {
        "engine": resolved_engine.value,
        "engine_display_name": display_name,
        "engine_binary": binary,
        "engine_pin": pin,
        "model": resolved_model,
        "small_fast_model": resolved_small,
        "reasoning_effort": reasoning_effort,
        "region": resolved_region,
        "workspace": str(workspace),
        "mcp_config_path": str(config_file),
        "mcp_servers": sorted(mcp_config["mcpServers"]),
        "gco_mcp_env": dict(sorted(gco_mcp_env.items())),
        "plugins": [str(path) for path in plugin_paths],
        "import_skills": [str(Path(item).expanduser()) for item in skills],
        "import_agents": [str(Path(item).expanduser()) for item in agents],
        "claude_binary": binary if resolved_engine is AutopilotEngine.CLAUDE_CODE else None,
        "claude_code_pin": (pin if resolved_engine is AutopilotEngine.CLAUDE_CODE else None),
        "codex_binary": binary if resolved_engine is AutopilotEngine.CODEX else None,
        "codex_pin": pin if resolved_engine is AutopilotEngine.CODEX else None,
        "install_command": " ".join(install_command),
        "resumable_session": resumable,
        "mcp_config": mcp_config,
        "codex_config": codex_config,
    }
    return plan, warnings


def _resolve_resume_args(
    plan: dict[str, Any],
    continue_session: bool,
    resume: str | None,
    engine_args: tuple[str, ...],
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
    if _CLAUDE_RESUME_FLAGS & set(engine_args):
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


def _resolve_codex_resume_args(
    continue_session: bool,
    resume: str | None,
) -> tuple[str, ...]:
    """Map generic resume options onto Codex's resume subcommand."""
    if continue_session:
        return ("resume", "--last")
    if resume is not None:
        if resume == "":
            return ("resume",)
        if resume.startswith("-"):
            return ("resume", "--", resume)
        return ("resume", resume)
    return ()


def _validate_codex_engine_args(engine_args: tuple[str, ...]) -> None:
    """Reject native overrides that would invalidate Autopilot's launch plan.

    GCO owns the Bedrock provider/model, project-layer isolation, update policy,
    and generated MCP process definitions. Native config may still tighten the
    GCO server to the two documentation tools used by the reviewed live demo.
    """
    direct_overrides = {
        "-m",
        "--model",
        "-p",
        "--profile",
        "--oss",
        "--local-provider",
        "-C",
        "--cd",
        "--remote",
        "--remote-auth-token-env",
        "update",
    }
    direct_prefixes = (
        "--model=",
        "--profile=",
        "--local-provider=",
        "--cd=",
        "--remote=",
        "--remote-auth-token-env=",
    )
    config_options = {"-c", "--config"}
    reserved_config_roots = {
        "check_for_update_on_startup",
        "mcp_servers",
        "model",
        "model_provider",
        "model_providers",
        "model_reasoning_effort",
        "profile",
        "profiles",
        "project_root_markers",
        "projects",
    }
    recorder_tools = {"find_docs", "read_resource"}

    def config_root(assignment: str) -> str | None:
        key_expression, separator, _value = assignment.partition("=")
        if not separator:
            return None
        try:
            parsed = tomllib.loads(f"{key_expression}=0")
        except tomllib.TOMLDecodeError:
            # Invalid TOML will fail in Codex itself, but retain a conservative
            # lexical fallback so malformed quoting cannot bypass an owned root.
            normalized = key_expression.strip().lstrip("\"'")
            return normalized.split(".", 1)[0].strip().rstrip("\"'") or None
        return next(iter(parsed), None)

    def is_safe_gco_tool_narrowing(assignment: str) -> bool:
        """Allow only the recorder's fail-closed GCO documentation overlay."""
        try:
            parsed = tomllib.loads(assignment)
        except tomllib.TOMLDecodeError:
            return False
        if set(parsed) != {"mcp_servers"}:
            return False
        servers = parsed["mcp_servers"]
        if not isinstance(servers, dict) or set(servers) != {"gco"}:
            return False
        gco = servers["gco"]
        if not isinstance(gco, dict) or not set(gco) <= {
            "enabled_tools",
            "required",
            "tools",
        }:
            return False
        if "required" in gco and gco["required"] is not True:
            return False
        if "enabled_tools" in gco:
            enabled = gco["enabled_tools"]
            if (
                not isinstance(enabled, list)
                or not enabled
                or not all(isinstance(tool, str) for tool in enabled)
                or not set(enabled) <= recorder_tools
            ):
                return False
        if "tools" in gco:
            tools = gco["tools"]
            if not isinstance(tools, dict) or not set(tools) <= recorder_tools:
                return False
            for policy in tools.values():
                if not isinstance(policy, dict) or policy != {"approval_mode": "approve"}:
                    return False
        return True

    index = 0
    while index < len(engine_args):
        argument = engine_args[index]
        if argument == "--":
            # Codex treats its own separator as the end of native options; the
            # remaining tokens are prompt text and must not be option-scanned.
            break

        attached_short_override = (
            (argument.startswith("-m") and argument != "-m")
            or (argument.startswith("-p") and argument != "-p")
            or (argument.startswith("-C") and argument != "-C")
        ) and not argument.startswith("--")
        if (
            argument in direct_overrides
            or argument.startswith(direct_prefixes)
            or attached_short_override
        ):
            raise ValueError(
                f"Codex passthrough option {argument!r} would override Autopilot's "
                "isolated Bedrock launch plan. Use the top-level `gco autopilot "
                "--model` option for model overrides; alternate projects, "
                "profiles, providers, remote sessions, and in-place updates are "
                "incompatible with this engine."
            )

        assignment: str | None = None
        option = argument
        if argument in config_options:
            if index + 1 < len(engine_args):
                assignment = engine_args[index + 1]
                index += 1
        elif argument.startswith("--config="):
            assignment = argument.removeprefix("--config=")
            option = "--config"
        elif argument.startswith("-c") and argument != "-c":
            assignment = argument[2:].removeprefix("=")
            option = "-c"

        root = config_root(assignment) if assignment is not None else None
        if (
            root == "mcp_servers"
            and assignment is not None
            and is_safe_gco_tool_narrowing(assignment)
        ):
            index += 1
            continue
        if root in reserved_config_roots and assignment is not None:
            key = assignment.partition("=")[0]
            raise ValueError(
                f"Codex passthrough option {option!r} cannot override {key!r}; "
                "that setting is owned by Autopilot's isolated Bedrock plan. "
                "Use top-level `--model` or context.bedrock.codex in cdk.json."
            )
        index += 1


def _print_dry_run(formatter: Any, plan: dict[str, Any]) -> None:
    """Render the launch plan as the table-format summary."""
    print()
    print("  GCO Autopilot — launch plan")
    print("  " + "-" * 68)
    print(f"  Engine:            {plan['engine_display_name']}")
    print(f"  Model (Bedrock):   {plan['model']}")
    if plan["reasoning_effort"]:
        print(f"  Reasoning effort:  {plan['reasoning_effort']}")
    if plan["small_fast_model"]:
        print(f"  Fast model:        {plan['small_fast_model']}")
    print(f"  AWS region:        {plan['region']}")
    print(f"  Workspace:         {plan['workspace']}")
    isolation = "--strict-mcp-config" if plan["engine"] == "claude-code" else "isolated CODEX_HOME"
    print(f"  MCP config:        {plan['mcp_config_path']}  ({isolation})")
    print(f"  MCP servers ({len(plan['mcp_servers'])}):   " + ", ".join(plan["mcp_servers"]))
    if plan["gco_mcp_env"]:
        rendered = ", ".join(f"{k}={v}" for k, v in plan["gco_mcp_env"].items())
        print(f"  GCO MCP env:       {rendered}")
    else:
        print("  GCO MCP env:       (none — default read-only toolset)")
    if plan["plugins"]:
        print(f"  Plugins:           {', '.join(plan['plugins'])}")
    if plan["engine"] == AutopilotEngine.CODEX.value and plan["import_skills"]:
        skills = ", ".join(plan["import_skills"])
        print(f"  Skills:            {skills}  (copied into isolated CODEX_HOME)")
    else:
        imports = [f"skills:{path}" for path in plan["import_skills"]] + [
            f"agents:{path}" for path in plan["import_agents"]
        ]
        if imports:
            print(f"  Imports:           {', '.join(imports)}  (staged as a session plugin)")
    if plan["engine_binary"]:
        print(f"  {plan['engine_display_name']}:       {plan['engine_binary']}")
    else:
        print(
            f"  {plan['engine_display_name']}:       not installed — will offer: "
            f"{plan['install_command']}"
        )
    if plan["resumable_session"]:
        print("  Previous session:  found — launch will offer to resume (or pass --continue)")
    elif plan["engine"] == "claude-code":
        print("  Previous session:  none for this workspace")
    else:
        print("  Previous session:  use --continue or --resume to reopen a Codex session")
    print("  " + "-" * 68)
    print("  Dry run only — nothing was written or launched.")
    print()


@click.command("autopilot")
@click.option(
    "--engine",
    type=click.Choice([engine.value for engine in AutopilotEngine], case_sensitive=False),
    default=None,
    help=(
        "Agent runtime (default: claude-code; env override: GCO_AUTOPILOT_ENGINE). "
        "Codex uses Amazon Bedrock and an isolated CODEX_HOME."
    ),
)
@click.option(
    "--model",
    "-m",
    default=None,
    help=(
        "Bedrock model or inference-profile id for the selected engine. "
        "Defaults: context.bedrock.claude_code_default_model_id or "
        "context.bedrock.codex_default_model_id. Codex env override: "
        "GCO_AUTOPILOT_CODEX_MODEL; shared fallback: GCO_AUTOPILOT_MODEL."
    ),
)
@click.option(
    "--small-fast-model",
    default=None,
    help=(
        "Optional Bedrock model for Claude Code's background/fast tasks "
        "(env override: GCO_AUTOPILOT_SMALL_FAST_MODEL; unset by default). "
        "Claude-only; Codex rejects fast-model configuration."
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
        "Claude-only; Codex rejects plugins."
    ),
)
@click.option(
    "--skills",
    "skills",
    multiple=True,
    metavar="DIR",
    help=(
        "Import a directory of skills (one subdirectory per skill, each "
        "with a SKILL.md) into the session (repeatable). Claude stages a "
        "session plugin; Codex copies skills into GCO's isolated CODEX_HOME."
    ),
)
@click.option(
    "--agents",
    "agents",
    multiple=True,
    metavar="DIR",
    help=(
        "Import a directory of Claude Code agent files (*.md subagent "
        "definitions) into the session (repeatable). Claude-only; Codex "
        "rejects agent imports."
    ),
)
@click.option(
    "--continue",
    "-c",
    "continue_session",
    is_flag=True,
    help="Resume the most recent session for the selected engine and workspace.",
)
@click.option(
    "--resume",
    "resume",
    is_flag=False,
    flag_value="",
    default=None,
    metavar="[SESSION_ID]",
    help=(
        "Resume a specific session by id, or open the selected engine's "
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
    help="Print the selected engine's generated MCP/agent config and exit.",
)
@click.option("--yes", "-y", is_flag=True, help="Install the selected engine without prompting.")
@click.argument("engine_args", nargs=-1, type=click.UNPROCESSED)
@pass_config
def autopilot(
    config: Any,
    engine: Any,
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
    engine_args: Any,
) -> None:
    """Launch a fully configured Claude Code or Codex session for GCO.

    Claude Code remains the default engine. Select Codex with ``--engine
    codex`` or ``GCO_AUTOPILOT_ENGINE=codex``. Both engines use Amazon
    Bedrock through your AWS credentials and receive the GCO MCP server plus
    the recommended companion servers.

    Each engine has an independent model default in ``cdk.json`` and supports
    ``--model`` / environment overrides. Codex uses GCO's isolated
    ``~/.gco/autopilot/codex`` home and official Amazon Bedrock provider
    configuration; Claude preserves its JSON config and strict MCP mode.

    If the selected CLI is absent, Autopilot offers to install its exact npm
    pin. Arguments after ``--`` pass through unchanged to that CLI.
    ``--continue`` and ``--resume`` map to the selected engine's native
    resume syntax; Claude also keeps its interactive previous-session prompt.

    GCO MCP feature flags gate opt-in tool groups. By default the session gets
    the read-only toolset; pass ``--enable`` per flag or ``--enable all-tools``.
    ``--mcp-env`` sets any other GCO server variable.

    ``--skills`` works with either engine. ``--plugin`` and ``--agents`` are
    Claude-only because they use Claude Code plugin and agent formats.

    \b
    Examples:
        gco autopilot
        gco autopilot --engine codex
        GCO_AUTOPILOT_ENGINE=codex gco autopilot --continue
        gco autopilot --resume
        gco autopilot -e mission -e infrastructure-deploy
        gco autopilot -e all-tools
        gco autopilot --mcp-env GCO_MCP_TOOL_SEARCH=bm25
        gco autopilot --skills ~/team-skills
        gco autopilot --skills ~/team-skills --agents ~/my-agents
        gco autopilot --plugin ~/plugins/incident-response
        gco autopilot -m global.anthropic.claude-sonnet-4-6
        gco autopilot --engine codex -m global.openai.gpt-5.6-terra
        gco autopilot --engine codex --print-config
        gco autopilot --dry-run
        gco autopilot -y -- --permission-mode plan

    \b
    Requirements:
        - AWS credentials with Bedrock model invocation access
        - The selected model enabled in the resolved AWS Region
        - npm only when the selected CLI is not already installed
    """
    formatter = get_output_formatter(config)

    if continue_session and resume is not None:
        formatter.print_error("Pass either --continue or --resume, not both.")
        sys.exit(1)

    try:
        resolved_engine = resolve_engine(engine)
        if resolved_engine is AutopilotEngine.CODEX:
            _validate_codex_engine_args(tuple(engine_args))
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
            engine=resolved_engine,
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
        if plan["engine"] == AutopilotEngine.CODEX.value:
            click.echo(plan["codex_config"], nl=False)
        else:
            # Preserve Claude's raw JSON machine-readable surface.
            click.echo(json.dumps(plan["mcp_config"], indent=2))
        return

    if dry_run:
        if config.output_format == "table":
            _print_dry_run(formatter, plan)
        else:
            formatter.print(
                {
                    key: value
                    for key, value in plan.items()
                    if key not in {"mcp_config", "codex_config"}
                }
            )
        return

    if plan["engine"] == AutopilotEngine.CODEX.value:
        codex_binary = plan["codex_binary"]
        if codex_binary is None:
            formatter.print_info(f"Codex is not installed (pinned: {plan['codex_pin']}).")
            if not yes and not click.confirm(f"Install it now with `{plan['install_command']}`?"):
                formatter.print_error(
                    "Codex is required for this engine. Install it manually with "
                    f"`{plan['install_command']}` and re-run `gco autopilot --engine codex`."
                )
                sys.exit(1)
            rc = install_codex()
            if rc == 127:
                formatter.print_error(
                    "npm was not found on PATH. The GCO dev container ships the required "
                    "Node.js/npm toolchain; rebuild it or install npm and re-run."
                )
                sys.exit(1)
            if rc != 0:
                formatter.print_error(f"`{plan['install_command']}` failed with exit code {rc}.")
                sys.exit(1)
            codex_binary = find_codex_binary()
            if codex_binary is None:
                formatter.print_error(
                    "Codex installed but the `codex` binary is not on PATH. Open a new "
                    "shell (or fix your npm global bin path) and re-run."
                )
                sys.exit(1)
        try:
            write_codex_config(str(plan["codex_config"]))
            stage_codex_skills(tuple(skills))
        except (OSError, ValueError) as e:
            formatter.print_error(f"Failed to prepare the Codex session: {e}")
            sys.exit(1)
        resume_args = _resolve_codex_resume_args(continue_session, resume)
        env = build_codex_env(plan["region"])
        owned_args = build_codex_owned_args(
            model=plan["model"],
            region=plan["region"],
            reasoning_effort=plan["reasoning_effort"],
            workspace=Path(plan["workspace"]),
        )
        argv = build_codex_launch_argv(
            codex_binary,
            root_args=owned_args,
            resume_args=resume_args,
            extra_args=tuple(engine_args),
        )
        details = f", provider={CODEX_BEDROCK_PROVIDER}"
        if plan["reasoning_effort"] is not None:
            details = f", reasoning={plan['reasoning_effort']}{details}"
        formatter.print_info(f"Launching Codex on Bedrock ({plan['model']}{details})...")
        try:
            rc = exec_codex(argv, env)
        except OSError as e:
            formatter.print_error(
                f"Failed to launch Codex at {argv[0]}: {e}. Reinstall with "
                f"`{' '.join(codex_install_command())}` and re-run."
            )
            sys.exit(1)
        sys.exit(rc)

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

    resume_args = _resolve_resume_args(plan, continue_session, resume, tuple(engine_args), yes)

    env = build_claude_env(plan["model"], plan["region"], plan["small_fast_model"])
    argv = build_launch_argv(claude_binary, written, tuple(engine_args), resume_args, plugin_args)

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
