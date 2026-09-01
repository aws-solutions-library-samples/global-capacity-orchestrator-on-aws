"""Autopilot: launch a configured Claude Code or Codex session against GCO.

``gco autopilot`` defaults to Claude Code for backward compatibility and can
select Codex with ``--engine codex`` or ``GCO_AUTOPILOT_ENGINE=codex``. Both
engines use the caller's AWS credentials, GCO's canonical Bedrock defaults,
the GCO MCP server, and the recommended companion MCP servers. Claude Code
keeps its generated JSON config plus ``--strict-mcp-config`` behavior. Codex
uses a generated TOML config and skills inside GCO's isolated
``~/.gco/autopilot/codex`` home, leaving personal ``~/.codex`` state alone.

Neither CLI is baked into the development container. Autopilot detects the
selected engine's binary and offers to install its exact npm pin lazily.

Scanner contract (``.github/scripts/lib_dependency_scan.sh``):

* ``extract_claude_code_pin`` and ``extract_codex_pin`` read
  :data:`CLAUDE_CODE_VERSION` and :data:`CODEX_VERSION` from this file with
  regexes — keep both as single-line, double-quoted assignments.
* ``extract_companion_mcp_packages`` pairs the ``registry=`` / ``package=``
  keywords inside each ``CompanionServer(`` block — keep those two fields
  on their own lines when editing the registry below.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

from gco.bedrock import (
    get_default_claude_code_model_id,
    get_default_codex_model_id,
    get_default_codex_reasoning_effort,
)

from . import __version__

#: Exact Claude Code release installed by ``gco autopilot`` when the
#: ``claude`` binary is absent. Pinned (never ``latest``) so installs are
#: reproducible; the monthly deps-scan reports drift against npm.
CLAUDE_CODE_VERSION = "2.1.252"

#: npm package that ships the ``claude`` binary.
CLAUDE_CODE_PACKAGE = "@anthropic-ai/claude-code"

#: Exact Codex CLI release installed by the Codex engine.
#: Keep this literal assignment scanner-friendly like CLAUDE_CODE_VERSION.
CODEX_VERSION = "0.152.0"

#: npm package that ships the ``codex`` binary.
CODEX_PACKAGE = "@openai/codex"

#: Built-in Codex provider that signs Bedrock Runtime requests and supports
#: geographic/global cross-Region inference profile IDs.
CODEX_BEDROCK_PROVIDER = "amazon-bedrock-runtime"

#: Parallel npx/uvx startup can include a first-use package download. Codex's
#: generated config gives every curated MCP server one bounded minute.
CODEX_MCP_STARTUP_TIMEOUT_SECONDS = 60.0

#: Engine override environment variable; Claude Code remains the compatibility default.
_ENGINE_ENV = "GCO_AUTOPILOT_ENGINE"
_CODEX_MODEL_ENV = "GCO_AUTOPILOT_CODEX_MODEL"


class AutopilotEngine(StrEnum):
    """Interactive agent runtimes supported by ``gco autopilot``."""

    CLAUDE_CODE = "claude-code"
    CODEX = "codex"


def resolve_engine(explicit: str | AutopilotEngine | None) -> AutopilotEngine:
    """Resolve engine flag > environment > backward-compatible Claude default."""
    if explicit is not None:
        raw: str | AutopilotEngine = explicit
    elif _ENGINE_ENV in os.environ:
        raw = os.environ[_ENGINE_ENV]
    else:
        return AutopilotEngine.CLAUDE_CODE
    if isinstance(raw, AutopilotEngine):
        return raw
    candidate = str(raw).strip().lower()
    try:
        return AutopilotEngine(candidate)
    except ValueError as exc:
        supported = ", ".join(engine.value for engine in AutopilotEngine)
        raise ValueError(f"Unknown autopilot engine {raw!r}. Choose one of: {supported}.") from exc


#: Where the generated session MCP config lands. Regenerated on every
#: launch, so hand edits do not survive — persistent customization belongs
#: in your own MCP config (see gco_mcp/README.md).
_CONFIG_DIR_ENV = "GCO_AUTOPILOT_CONFIG_DIR"
_DEFAULT_CONFIG_DIR = Path.home() / ".gco" / "autopilot"
_CONFIG_FILENAME = "mcp.json"
_CODEX_HOME_DIRNAME = "codex"
_CODEX_CONFIG_FILENAME = "config.toml"
_CODEX_SKILLS_DIRNAME = "skills"

#: Model override environment variable (the ``--model`` flag wins over it).
_MODEL_ENV = "GCO_AUTOPILOT_MODEL"

#: Optional Bedrock model for Claude Code's background/fast tasks. Left
#: unset by default: the right haiku-class profile depends on what the
#: account has enabled, and Claude Code degrades gracefully without it.
_SMALL_FAST_MODEL_ENV = "GCO_AUTOPILOT_SMALL_FAST_MODEL"

#: Placeholder in companion ``args`` replaced with the launch directory.
_WORKSPACE_PLACEHOLDER = "{workspace}"

#: Colon-separated plugin dirs/zips always loaded into autopilot sessions
#: (merged with per-launch ``--plugin`` flags).
_PLUGIN_DIRS_ENV = "GCO_AUTOPILOT_PLUGIN_DIRS"

#: Name of the synthetic session plugin that packages loose ``--skills`` /
#: ``--agents`` directories so Claude Code can load them.
_IMPORTS_PLUGIN_NAME = "gco-autopilot-imports"

#: Read-only command allowlist for the shell companion. Deliberately tight —
#: no ``rm``, no ``git`` — matching the guidance in gco_mcp/README.md.
_SHELL_ALLOW_COMMANDS = "ls,cat,pwd,grep,wc,touch,find"


@dataclass(frozen=True)
class CompanionServer:
    """One recommended companion MCP server from ``gco_mcp/README.md``.

    ``registry`` + ``package`` identify the distribution (``npm`` or
    ``pypi``) for the deps-scan liveness check; ``command`` + ``args`` +
    ``env`` are the stdio launch recipe written into the session config.
    """

    name: str
    registry: str
    package: str
    command: str
    args: tuple[str, ...]
    env: dict[str, str] = field(default_factory=dict)


#: The companion MCP servers wired into every autopilot session, one entry
#: per row of the "Recommended Companion MCP Servers" tables in
#: ``gco_mcp/README.md``. ``tests/test_cli_autopilot.py`` enforces that the
#: two stay in lockstep, and the monthly deps-scan verifies each package is
#: still published (and not deprecated/yanked) on its registry.
#:
#: The EKS server is configured read-only on purpose: an auto-generated
#: agent session should not silently hold cluster write access. Add
#: ``--allow-write`` / ``--allow-sensitive-data-access`` in your own MCP
#: config if you want the mutating tools.
COMPANION_MCP_SERVERS: tuple[CompanionServer, ...] = (
    CompanionServer(
        name="aws-docs",
        registry="pypi",
        package="awslabs.aws-documentation-mcp-server",
        command="uvx",
        args=("awslabs.aws-documentation-mcp-server@latest",),
        env={"FASTMCP_LOG_LEVEL": "ERROR"},
    ),
    CompanionServer(
        name="aws-pricing",
        registry="pypi",
        package="awslabs.aws-pricing-mcp-server",
        command="uvx",
        args=("awslabs.aws-pricing-mcp-server@latest",),
        env={"FASTMCP_LOG_LEVEL": "ERROR"},
    ),
    CompanionServer(
        name="eks",
        registry="pypi",
        package="awslabs.eks-mcp-server",
        command="uvx",
        args=("awslabs.eks-mcp-server@latest",),
        env={"FASTMCP_LOG_LEVEL": "ERROR"},
    ),
    CompanionServer(
        name="filesystem",
        registry="npm",
        package="@modelcontextprotocol/server-filesystem",
        command="npx",
        args=("-y", "@modelcontextprotocol/server-filesystem", _WORKSPACE_PLACEHOLDER),
    ),
    CompanionServer(
        name="ddg-search",
        registry="pypi",
        package="duckduckgo-mcp-server",
        command="uvx",
        args=("duckduckgo-mcp-server",),
    ),
    CompanionServer(
        name="deepwiki",
        registry="npm",
        package="mcp-deepwiki",
        command="npx",
        args=("-y", "mcp-deepwiki@latest"),
    ),
    CompanionServer(
        name="playwright",
        registry="npm",
        package="@playwright/mcp",
        command="npx",
        args=("-y", "@playwright/mcp@latest"),
    ),
    CompanionServer(
        name="sequential-thinking",
        registry="npm",
        package="@modelcontextprotocol/server-sequential-thinking",
        command="npx",
        args=("-y", "@modelcontextprotocol/server-sequential-thinking"),
    ),
    CompanionServer(
        name="inner-monologue",
        registry="npm",
        package="inner-monologue-mcp",
        command="npx",
        args=("-y", "inner-monologue-mcp"),
    ),
    CompanionServer(
        name="memory",
        registry="npm",
        package="@modelcontextprotocol/server-memory",
        command="npx",
        args=("-y", "@modelcontextprotocol/server-memory"),
    ),
    CompanionServer(
        name="mcp-tasks",
        registry="npm",
        package="mcp-tasks",
        command="npx",
        args=("-y", "mcp-tasks"),
    ),
    CompanionServer(
        name="shell",
        registry="pypi",
        package="mcp-shell-server",
        command="uvx",
        args=("mcp-shell-server",),
        env={"ALLOW_COMMANDS": _SHELL_ALLOW_COMMANDS},
    ),
)


def _source_checkout_root(candidate: Path | None = None) -> Path | None:
    """Return the GCO checkout root when autopilot runs from source.

    Mirrors the marker discipline in :mod:`gco.bedrock`: only the checkout
    that owns *this* file counts (``candidate`` exists for tests) — the
    current working directory is deliberately ignored so an unrelated
    project cannot redirect which MCP server code the session runs.
    """
    root = candidate if candidate is not None else Path(__file__).resolve().parent.parent
    markers = (root / "app.py", root / "pyproject.toml", root / "gco_mcp" / "run_mcp.py")
    if all(marker.is_file() for marker in markers):
        return root
    return None


#: Every feature flag the GCO MCP server understands: the umbrella flag
#: first (a perfectly reasonable thing to pass to ``--enable``), then the
#: per-tool flags. This mirrors ``gco_mcp/feature_flags.py`` rather than
#: importing it — the CLI must not depend on the MCP package at runtime
#: (mypy also maps the PEP 420 namespace file under two module names when
#: both trees are checked together). ``tests/test_cli_autopilot.py`` holds
#: the two registries in lockstep, so drift fails the PR that introduces it.
_KNOWN_GCO_MCP_FLAGS: tuple[str, ...] = (
    "GCO_ENABLE_ALL_TOOLS",
    "GCO_ENABLE_CAPACITY_PURCHASE",
    "GCO_ENABLE_MODEL_UPLOAD",
    "GCO_ENABLE_IMAGE_PUBLISH",
    "GCO_ENABLE_INFRASTRUCTURE_DEPLOY",
    "GCO_ENABLE_INFRASTRUCTURE_DESTROY",
    "GCO_ENABLE_DESTRUCTIVE_OPERATIONS",
    "GCO_ENABLE_MISSION",
    "GCO_ENABLE_LOCAL_METRICS",
    "GCO_ENABLE_LOCAL_STORAGE_SYNC",
    "GCO_ENABLE_SEMANTIC_PROGRESS",
    "GCO_ENABLE_CONFIG_MANAGEMENT",
    "GCO_ENABLE_SWARM",
)


def known_gco_mcp_flags() -> tuple[str, ...]:
    """Return every feature flag the GCO MCP server understands."""
    return _KNOWN_GCO_MCP_FLAGS


def resolve_mcp_flags(enable: tuple[str, ...]) -> dict[str, str]:
    """Translate ``--enable`` values into env vars for the gco MCP server.

    Accepts either the full env-var form (``GCO_ENABLE_MISSION``) or the
    bare suffix (``mission``, ``all-tools``, ``ALL_TOOLS``) and normalizes
    to the canonical name. Unknown flags raise ``ValueError`` listing the
    valid set — a typo should fail loudly at launch, not silently launch a
    session missing the tools the caller asked for.
    """
    known = known_gco_mcp_flags()
    by_name = {flag: flag for flag in known}
    by_suffix = {flag.removeprefix("GCO_ENABLE_"): flag for flag in known}

    resolved: dict[str, str] = {}
    for raw in enable:
        candidate = raw.strip().upper().replace("-", "_")
        flag = by_name.get(candidate) or by_suffix.get(candidate)
        if flag is None:
            suffixes = ", ".join(sorted(s.lower().replace("_", "-") for s in by_suffix))
            raise ValueError(
                f"Unknown GCO MCP feature flag {raw!r}. Valid flags: {suffixes} "
                "(or their full GCO_ENABLE_* names)."
            )
        resolved[flag] = "true"
    return resolved


def _gco_server_entry(mcp_env: dict[str, str] | None = None) -> dict[str, object]:
    """Build the ``gco`` MCP server entry for the session config.

    From a source checkout the server runs straight off the working tree
    (``python3 gco_mcp/run_mcp.py``) so local MCP changes are live. From an
    installed ``gco-cli`` it runs the release tag matching ``__version__``
    via ``uvx`` — the same no-clone form gco_mcp/README.md documents.

    ``mcp_env`` carries feature flags (``GCO_ENABLE_*``) and any other
    server environment (for example ``GCO_MCP_TOOL_SEARCH``) into the
    server process.
    """
    checkout = _source_checkout_root()
    entry: dict[str, object]
    if checkout is not None:
        entry = {
            "command": sys.executable or "python3",
            "args": [str(checkout / "gco_mcp" / "run_mcp.py")],
        }
    else:
        entry = {
            "command": "uvx",
            "args": [
                "--python",
                "3.14",
                "--from",
                "git+https://github.com/aws-solutions-library-samples/global-capacity-orchestrator-on-aws.git"
                f"@v{__version__}",
                "gco-mcp",
            ],
        }
    if mcp_env:
        entry["env"] = dict(sorted(mcp_env.items()))
    return entry


def build_mcp_config(
    workspace: Path,
    include_companions: bool = True,
    gco_mcp_env: dict[str, str] | None = None,
) -> dict[str, dict[str, dict[str, object]]]:
    """Return the session ``mcpServers`` config for Claude Code.

    ``workspace`` replaces the ``{workspace}`` placeholder in companion
    args (the filesystem server's root), so file access is scoped to the
    directory autopilot was launched from. ``gco_mcp_env`` is applied to
    the gco server entry only — feature flags gate GCO tools, not the
    companions.
    """
    servers: dict[str, dict[str, object]] = {"gco": _gco_server_entry(gco_mcp_env)}
    if include_companions:
        for companion in COMPANION_MCP_SERVERS:
            entry: dict[str, object] = {
                "command": companion.command,
                "args": [
                    str(workspace) if arg == _WORKSPACE_PLACEHOLDER else arg
                    for arg in companion.args
                ],
            }
            if companion.env:
                entry["env"] = dict(companion.env)
            servers[companion.name] = entry
    return {"mcpServers": servers}


def config_path() -> Path:
    """Return the on-disk location of the generated session MCP config."""
    override = os.environ.get(_CONFIG_DIR_ENV)
    directory = Path(override).expanduser() if override else _DEFAULT_CONFIG_DIR
    return directory / _CONFIG_FILENAME


def write_mcp_config(config: dict[str, dict[str, dict[str, object]]]) -> Path:
    """Write the session MCP config and return its path."""
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    return path


def codex_home() -> Path:
    """Return GCO's isolated, persistent Codex home directory."""
    return config_path().parent / _CODEX_HOME_DIRNAME


def codex_config_path() -> Path:
    """Return the generated Codex config path under the isolated home."""
    return codex_home() / _CODEX_CONFIG_FILENAME


def _toml_string(value: str) -> str:
    """Encode one TOML basic string using JSON's compatible escaping."""
    return json.dumps(value, ensure_ascii=False)


def _toml_array(values: list[str]) -> str:
    return "[" + ", ".join(_toml_string(value) for value in values) + "]"


def build_codex_config_toml(
    mcp_config: dict[str, dict[str, dict[str, object]]],
    *,
    model: str,
    region: str,
    reasoning_effort: str | None,
) -> str:
    """Render a complete isolated Codex config with Bedrock and MCP servers.

    ``model_reasoning_effort`` is emitted only for the canonical GCO model.
    Explicit CLI/environment model overrides must use the selected model's own
    defaults instead of inheriting a potentially incompatible effort value.
    """
    lines = [
        f"model = {_toml_string(model)}",
        f"model_provider = {_toml_string(CODEX_BEDROCK_PROVIDER)}",
    ]
    if reasoning_effort is not None:
        lines.append(f"model_reasoning_effort = {_toml_string(reasoning_effort)}")
    lines.extend(
        [
            "check_for_update_on_startup = false",
            "",
            f"[model_providers.{CODEX_BEDROCK_PROVIDER}]",
            'wire_api = "responses"',
            "",
            f"[model_providers.{CODEX_BEDROCK_PROVIDER}.aws]",
            f"region = {_toml_string(region)}",
        ]
    )
    servers = mcp_config["mcpServers"]
    for name in sorted(servers):
        entry = servers[name]
        command = entry.get("command")
        args = entry.get("args", [])
        if not isinstance(command, str) or not command:
            raise ValueError(f"Codex MCP server {name!r} has no command")
        if not isinstance(args, list) or not all(isinstance(item, str) for item in args):
            raise ValueError(f"Codex MCP server {name!r} args must be strings")
        table_name = _toml_string(name)
        lines.extend(
            [
                "",
                f"[mcp_servers.{table_name}]",
                f"command = {_toml_string(command)}",
                f"args = {_toml_array(args)}",
                "enabled = true",
                f"startup_timeout_sec = {CODEX_MCP_STARTUP_TIMEOUT_SECONDS}",
            ]
        )
        environment = entry.get("env")
        if environment:
            if not isinstance(environment, dict) or not all(
                isinstance(key, str) and isinstance(value, str)
                for key, value in environment.items()
            ):
                raise ValueError(f"Codex MCP server {name!r} env must contain strings")
            lines.extend(["", f"[mcp_servers.{table_name}.env]"])
            for key, value in sorted(environment.items()):
                lines.append(f"{_toml_string(key)} = {_toml_string(value)}")
    return "\n".join(lines) + "\n"


def write_codex_config(content: str) -> Path:
    """Write the generated Codex configuration inside isolated CODEX_HOME."""
    path = codex_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def _selected_model_override(
    explicit: str | None,
    env_names: tuple[str, ...],
) -> tuple[str | None, str | None]:
    """Return the highest-precedence model override and its user-facing source.

    Presence, not truthiness, selects a source so a blank high-precedence value
    fails closed instead of silently falling through to another model.
    """
    if explicit is not None:
        raw_value = explicit
        source = "--model"
    else:
        for env_name in env_names:
            if env_name in os.environ:
                raw_value = os.environ[env_name]
                source = env_name
                break
        else:
            return None, None
    value = raw_value.strip()
    if not value:
        raise ValueError(f"{source} must be a non-empty Bedrock model id")
    return value, source


def resolve_model(explicit: str | None) -> tuple[str, list[str]]:
    """Resolve the Claude Bedrock model with strict override validation.

    Precedence: ``--model`` flag > ``GCO_AUTOPILOT_MODEL`` env > the
    ``cdk.json`` Claude Code default
    (``context.bedrock.claude_code_default_model_id``, deliberately separate
    from the advisory default Mission and the capacity advisor share). The
    result is advisory-validated only: Bedrock ids for Claude contain
    ``anthropic``/``claude``, but application inference-profile ARNs are
    opaque, so an unfamiliar id produces a warning rather than a refusal.
    """
    warnings: list[str] = []
    override, _source = _selected_model_override(explicit, (_MODEL_ENV,))
    model = override if override is not None else get_default_claude_code_model_id()
    lowered = model.lower()
    if "anthropic" not in lowered and "claude" not in lowered:
        warnings.append(
            f"Model id {model!r} does not look like a Claude model on Bedrock. "
            "Claude Code is tuned for Claude models; continuing anyway."
        )
    return model, warnings


def resolve_codex_model(explicit: str | None) -> tuple[str, list[str]]:
    """Resolve Codex model: flag > Codex env > generic env > cdk.json."""
    warnings: list[str] = []
    override, _source = _selected_model_override(
        explicit,
        (_CODEX_MODEL_ENV, _MODEL_ENV),
    )
    model = override if override is not None else get_default_codex_model_id()
    lowered = model.lower()
    if "openai" not in lowered and "gpt" not in lowered:
        warnings.append(
            f"Model id {model!r} does not look like an OpenAI model on Bedrock; "
            "continuing with the explicit override."
        )
    return model, warnings


def resolve_codex_reasoning_effort(explicit_model: str | None = None) -> str | None:
    """Return canonical Codex effort only when no model override was selected."""
    override, _source = _selected_model_override(
        explicit_model,
        (_CODEX_MODEL_ENV, _MODEL_ENV),
    )
    if override is not None:
        return None
    return get_default_codex_reasoning_effort()


def resolve_small_fast_model(explicit: str | None) -> str | None:
    """Resolve Claude's optional fast model, rejecting configured blank values."""
    if explicit is not None:
        raw_value = explicit
        source = "--small-fast-model"
    elif _SMALL_FAST_MODEL_ENV in os.environ:
        raw_value = os.environ[_SMALL_FAST_MODEL_ENV]
        source = _SMALL_FAST_MODEL_ENV
    else:
        return None
    value = raw_value.strip()
    if not value:
        raise ValueError(f"{source} must be a non-empty Bedrock model id")
    return value


def build_claude_env(
    model: str,
    region: str,
    small_fast_model: str | None = None,
) -> dict[str, str]:
    """Return the environment for the Claude Code process.

    Starts from the caller's environment so AWS credentials, profiles, and
    proxies pass through untouched. An ``AWS_REGION`` already set by the
    caller wins over the GCO-configured region — least surprise for anyone
    juggling AWS environments — and ``ANTHROPIC_SMALL_FAST_MODEL`` is only
    set when a background model was explicitly chosen.
    """
    env = dict(os.environ)
    env["CLAUDE_CODE_USE_BEDROCK"] = "1"
    env["DISABLE_AUTOUPDATER"] = "1"
    env["ANTHROPIC_MODEL"] = model
    env.setdefault("AWS_REGION", region)
    if small_fast_model:
        env["ANTHROPIC_SMALL_FAST_MODEL"] = small_fast_model
    return env


def build_codex_env(region: str) -> dict[str, str]:
    """Return an isolated Codex environment while preserving AWS credentials."""
    env = dict(os.environ)
    env.setdefault("AWS_REGION", region)
    env["CODEX_HOME"] = str(codex_home())
    return env


def effective_aws_region(default_region: str) -> str:
    """Resolve the AWS SDK region Codex and its generated config should share."""
    return os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION") or default_region


def find_claude_binary() -> str | None:
    """Return the resolved ``claude`` executable path, or ``None``."""
    return shutil.which("claude")


def find_codex_binary() -> str | None:
    """Return the resolved ``codex`` executable path, or ``None``."""
    return shutil.which("codex")


def plugin_paths_requested(cli_plugins: tuple[str, ...]) -> bool:
    """Return whether CLI or environment requested a Claude Code plugin."""
    if cli_plugins:
        return True
    return any(part.strip() for part in os.environ.get(_PLUGIN_DIRS_ENV, "").split(":"))


def resolve_plugin_paths(cli_plugins: tuple[str, ...]) -> list[Path]:
    """Resolve the plugin dirs/zips this session loads, validating each.

    Merges ``--plugin`` flags with the ``GCO_AUTOPILOT_PLUGIN_DIRS``
    environment variable (colon-separated, for the "always bring my team's
    plugin" case). A missing path raises ``ValueError`` — silently launching
    without the skills someone asked for is the failure mode this guards.
    """
    raw: list[str] = list(cli_plugins)
    env_value = os.environ.get(_PLUGIN_DIRS_ENV, "")
    raw.extend(part for part in env_value.split(":") if part.strip())

    resolved: list[Path] = []
    seen: set[Path] = set()
    for item in raw:
        path = Path(item).expanduser()
        if not path.exists():
            raise ValueError(f"Plugin path does not exist: {path}")
        path = path.resolve()
        if path not in seen:
            seen.add(path)
            resolved.append(path)
    return resolved


def validate_imports(
    skills_dirs: tuple[str, ...],
    agents_dirs: tuple[str, ...],
) -> None:
    """Validate ``--skills`` / ``--agents`` sources without staging anything.

    Shared by the dry-run/plan path (which must not write) and by
    :func:`stage_imports`. ``skills_dirs`` entries must contain at least one
    ``*/SKILL.md``; ``agents_dirs`` entries at least one ``*.md``. Anything
    else raises ``ValueError`` — an empty import is a typo'd path, not a
    preference.
    """
    for kind, sources, marker in (
        ("skills", skills_dirs, "*/SKILL.md"),
        ("agents", agents_dirs, "*.md"),
    ):
        for source_raw in sources:
            source = Path(source_raw).expanduser()
            if not source.is_dir():
                raise ValueError(f"--{kind} path is not a directory: {source}")
            if not any(source.glob(marker)):
                raise ValueError(
                    f"--{kind} directory {source} contains no {marker} — "
                    "check the path (skills are one subdirectory per skill "
                    "with a SKILL.md; agents are *.md files)."
                )


def stage_imports(
    skills_dirs: tuple[str, ...],
    agents_dirs: tuple[str, ...],
) -> Path | None:
    """Package loose skills/agents directories as a session plugin.

    Claude Code loads skills and agents from ``~/.claude`` and the
    workspace's ``.claude`` automatically; this exists for everything
    *else* — a team repo of skills, a scratch directory of agent files —
    without copying anything into the user's project or personal config.
    The staged plugin lives next to the generated MCP config, is rebuilt
    from scratch on every launch (hand edits do not survive), and is
    handed to claude with ``--plugin-dir``.

    Sources are validated by :func:`validate_imports` first. Returns the
    plugin directory, or ``None`` when nothing was imported.
    """
    if not skills_dirs and not agents_dirs:
        return None
    validate_imports(skills_dirs, agents_dirs)

    plugin_root = config_path().parent / _IMPORTS_PLUGIN_NAME
    shutil.rmtree(plugin_root, ignore_errors=True)
    manifest_dir = plugin_root / ".claude-plugin"
    manifest_dir.mkdir(parents=True)
    manifest = {
        "name": _IMPORTS_PLUGIN_NAME,
        "version": __version__,
        "description": (
            "Session-scoped skills/agents imported by `gco autopilot "
            "--skills/--agents`. Regenerated on every launch."
        ),
    }
    (manifest_dir / "plugin.json").write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
    )

    for kind, sources in (("skills", skills_dirs), ("agents", agents_dirs)):
        destination = plugin_root / kind
        for source_raw in sources:
            source = Path(source_raw).expanduser()
            shutil.copytree(source, destination, dirs_exist_ok=True)
    return plugin_root


def stage_codex_skills(skills_dirs: tuple[str, ...]) -> Path | None:
    """Rebuild GCO's isolated Codex skills directory from validated sources."""
    destination = codex_home() / _CODEX_SKILLS_DIRNAME
    shutil.rmtree(destination, ignore_errors=True)
    if not skills_dirs:
        return None
    validate_imports(skills_dirs, ())
    destination.mkdir(parents=True)
    for source_raw in skills_dirs:
        shutil.copytree(
            Path(source_raw).expanduser(),
            destination,
            dirs_exist_ok=True,
        )
    return destination


def build_plugin_args(plugin_paths: list[Path]) -> tuple[str, ...]:
    """Render plugin paths as claude's repeatable ``--plugin-dir`` flags."""
    args: list[str] = []
    for path in plugin_paths:
        args.extend(("--plugin-dir", str(path)))
    return tuple(args)


#: Where Claude Code keeps per-project conversation transcripts.
_CLAUDE_PROJECTS_DIR = Path.home() / ".claude" / "projects"


def _claude_project_dir_name(workspace: Path) -> str:
    """Return Claude Code's directory name for a workspace path.

    Claude Code names each entry under ``~/.claude/projects`` by replacing
    every non-alphanumeric character of the absolute workspace path with
    ``-`` (so ``/Users/dev/my_repo`` becomes ``-Users-dev-my-repo``).
    """
    return re.sub(r"[^A-Za-z0-9]", "-", str(workspace))


def has_resumable_session(workspace: Path) -> bool:
    """Return whether Claude Code has a previous session for ``workspace``.

    Peeks at Claude Code's own transcript store (one ``*.jsonl`` per
    conversation). The layout is Claude Code internal, so this check is
    deliberately fail-quiet: if the directory scheme ever changes, the
    resume prompt silently stops appearing while the explicit
    ``--continue`` / ``--resume`` flags — which claude interprets itself —
    keep working unchanged.
    """
    try:
        project_dir = _CLAUDE_PROJECTS_DIR / _claude_project_dir_name(workspace)
        return any(project_dir.glob("*.jsonl"))
    except OSError:
        return False


def claude_install_command() -> list[str]:
    """Return the pinned, reproducible Claude Code install command.

    ``--allow-scripts`` names exactly this one package: Claude Code's
    postinstall downloads the platform-native binary, and npm >= 12 blocks
    lifecycle scripts by default, which would otherwise leave a shim on
    PATH that fails with ``Exec format error`` on launch. Older npm (< 12)
    accepts and ignores the flag, so one command form works everywhere.
    """
    return [
        "npm",
        "install",
        "-g",
        f"--allow-scripts={CLAUDE_CODE_PACKAGE}",
        f"{CLAUDE_CODE_PACKAGE}@{CLAUDE_CODE_VERSION}",
    ]


def install_claude_code() -> int:
    """Install the pinned Claude Code release; return the npm exit code."""
    if shutil.which("npm") is None:
        return 127
    return subprocess.call(claude_install_command())  # noqa: S603


def codex_install_command() -> list[str]:
    """Return the exact pinned Codex npm install command."""
    return ["npm", "install", "-g", f"{CODEX_PACKAGE}@{CODEX_VERSION}"]


def install_codex() -> int:
    """Install the pinned Codex release; return the npm exit code."""
    if shutil.which("npm") is None:
        return 127
    return subprocess.call(codex_install_command())  # noqa: S603


def build_launch_argv(
    claude_binary: str,
    mcp_config: Path,
    extra_args: tuple[str, ...] = (),
    resume_args: tuple[str, ...] = (),
    plugin_args: tuple[str, ...] = (),
) -> list[str]:
    """Return the Claude Code argv for a hermetic autopilot session.

    ``--strict-mcp-config`` makes the generated config the *only* MCP
    config: personal ``~/.claude`` servers and project ``.mcp.json`` files
    are ignored, so every autopilot session starts from the same known-good
    server set. ``resume_args`` carries claude's native session-resumption
    flags (``--continue`` / ``--resume [id]``) when the caller asked to
    pick up an earlier conversation; the resumed session still runs under
    this launch's MCP config and Bedrock environment. ``plugin_args``
    carries the ``--plugin-dir`` flags for session-scoped plugins and the
    staged skills/agents imports.
    """
    return [
        claude_binary,
        "--mcp-config",
        str(mcp_config),
        "--strict-mcp-config",
        *plugin_args,
        *resume_args,
        *extra_args,
    ]


def codex_project_root(workspace: Path) -> Path:
    """Return Codex's normalized project root for a run-scoped trust policy.

    Codex 0.150.1 loads a trusted project's ``.codex/config.toml`` above its
    user config, even when ``CODEX_HOME`` is isolated. Its Git project identity
    follows the common Git directory, so linked worktrees resolve to the main
    checkout. Outside Git, the launch directory itself is the project root.
    """
    resolved = workspace.resolve()
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
            cwd=resolved,
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
    except OSError, subprocess.TimeoutExpired:
        return resolved
    if result.returncode != 0 or not result.stdout.strip():
        return resolved
    return Path(result.stdout.strip()).resolve().parent


def build_codex_owned_args(
    *,
    model: str,
    region: str,
    reasoning_effort: str | None,
    workspace: Path,
) -> tuple[str, ...]:
    """Return session-precedence controls that keep the Codex plan authoritative.

    ``CODEX_HOME`` isolates personal user state, but Codex 0.150.1 otherwise
    layers trusted project configuration above the generated TOML. Marking the
    discovered project root untrusted for this launch disables that project
    layer without persisting trust state. Scalar Bedrock/update controls are
    repeated at session precedence as defense in depth. Organization-managed
    policy remains authoritative by Codex design.
    """
    project_root = codex_project_root(workspace)
    assignments = [
        f"model_provider={_toml_string(CODEX_BEDROCK_PROVIDER)}",
        (f"model_providers.{CODEX_BEDROCK_PROVIDER}.wire_api={_toml_string('responses')}"),
        (f"model_providers.{CODEX_BEDROCK_PROVIDER}.aws.region={_toml_string(region)}"),
        "check_for_update_on_startup=false",
        (f'projects={{{_toml_string(str(project_root))}={{trust_level="untrusted"}}}}'),
    ]
    if reasoning_effort is not None:
        assignments.insert(
            1,
            f"model_reasoning_effort={_toml_string(reasoning_effort)}",
        )

    args = ["--model", model]
    for assignment in assignments:
        args.extend(("-c", assignment))
    return tuple(args)


def build_codex_launch_argv(
    codex_binary: str,
    *,
    root_args: tuple[str, ...] = (),
    resume_args: tuple[str, ...] = (),
    extra_args: tuple[str, ...] = (),
) -> list[str]:
    """Return Codex argv in native root → resume-selector → passthrough order."""
    return [codex_binary, *root_args, *resume_args, *extra_args]


def exec_claude(argv: list[str], env: dict[str, str]) -> int:
    """Hand the terminal over to Claude Code.

    On POSIX the process image is replaced (``execvpe``) so the terminal
    *becomes* the session — no wrapper process lingers, signals and TTY
    behavior are exactly claude's own. ``execvpe`` does not return on
    success. Windows has no true exec, so the session runs as a child
    process and its exit code is propagated.
    """
    if sys.platform == "win32":
        return subprocess.call(argv, env=env)  # noqa: S603
    os.execvpe(argv[0], argv, env)  # noqa: S606
    raise AssertionError("unreachable: execvpe replaces the process on success")


def exec_codex(argv: list[str], env: dict[str, str]) -> int:
    """Hand the terminal over to Codex using the same process semantics."""
    return exec_claude(argv, env)
