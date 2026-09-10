"""Single source for the Autopilot facts and assertions CI jobs share.

Unit, dev-container, and boot jobs use this module instead of duplicating
expected models, pins, generated-config shapes, or companion-server lists.
Every fact is derived from production modules, so no release literal lives
here to update separately.

Print subcommands accept ``--engine`` (default: ``claude-code``):

    pin                         selected engine's pinned CLI version
    install-command             exact lazy npm install command
    default-model               shipped Bedrock default model id
    expected-servers            expected MCP server names, one per line

Verification subcommands:

    verify-config PATH          validate Claude's --print-config JSON
    verify-codex-config PATH    validate Codex's --print-config TOML
        [--no-companions]         expect only the gco server
        [--expect-gco-env K=V]    require a gco-only env pair (repeatable)
        [--gco-args ARG]          require exact gco args (repeatable, ordered)
    verify-plan PATH            validate a -o json --dry-run plan
        [--engine ENGINE]
        [--claude-binary present|absent]
        [--codex-binary present|absent]

Importable for pytest: ``expected_servers()``, ``verify_config()``,
``verify_codex_config()``, ``verify_plan()``, ``main()``.
"""

from __future__ import annotations

import argparse
import json
import sys
import tomllib
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from cli.autopilot import (  # noqa: E402
    CLAUDE_CODE_PACKAGE,
    CLAUDE_CODE_VERSION,
    CODEX_BEDROCK_PROVIDER,
    CODEX_MCP_STARTUP_TIMEOUT_SECONDS,
    CODEX_PACKAGE,
    CODEX_VERSION,
    COMPANION_MCP_SERVERS,
    AutopilotEngine,
    claude_install_command,
    codex_install_command,
)
from gco.bedrock import (  # noqa: E402
    get_default_claude_code_model_id,
    get_default_codex_model_id,
    get_default_codex_reasoning_effort,
)

#: Companions deliberately pruned from the curated registry; their names
#: must never reappear anywhere in a generated session config.
PRUNED_PACKAGES: tuple[str, ...] = ("mcp-server-fetch", "mcp-server-calculator")
_ENGINE_CHOICES = tuple(engine.value for engine in AutopilotEngine)


def expected_servers(include_companions: bool = True) -> list[str]:
    """Return the exact MCP server set a generated session config must carry."""
    names = {"gco"}
    if include_companions:
        names |= {companion.name for companion in COMPANION_MCP_SERVERS}
    return sorted(names)


def _verify_server_mapping(
    servers: Any,
    *,
    include_companions: bool,
    expect_gco_env: dict[str, str] | None,
    gco_args: list[str] | None,
) -> list[str]:
    if not isinstance(servers, dict):
        return ["config carries no MCP server mapping"]

    problems: list[str] = []
    expected = expected_servers(include_companions)
    actual = sorted(servers)
    if actual != expected:
        missing = sorted(set(expected) - set(actual))
        extra = sorted(set(actual) - set(expected))
        problems.append(f"server set mismatch: missing={missing} unexpected={extra}")

    for name, entry in sorted(servers.items()):
        if not isinstance(entry, dict):
            problems.append(f"{name}: entry must be a mapping, got {entry!r}")
            continue
        command = entry.get("command")
        if not isinstance(command, str) or not command:
            problems.append(f"{name}: command must be a non-empty string, got {command!r}")
        args = entry.get("args")
        if not isinstance(args, list) or not all(isinstance(arg, str) for arg in args):
            problems.append(f"{name}: args must be a list of strings, got {args!r}")

    text = json.dumps(servers)
    for pruned in PRUNED_PACKAGES:
        if pruned in text:
            problems.append(f"pruned package {pruned!r} reappeared in the generated config")

    gco_entry = servers.get("gco", {})
    if not isinstance(gco_entry, dict):
        gco_entry = {}
    if expect_gco_env is not None:
        gco_env = gco_entry.get("env", {})
        if not isinstance(gco_env, dict):
            gco_env = {}
        for key, value in sorted(expect_gco_env.items()):
            if gco_env.get(key) != value:
                problems.append(f"gco env {key!r}: expected {value!r}, got {gco_env.get(key)!r}")
        for name, entry in sorted(servers.items()):
            if name == "gco" or not isinstance(entry, dict):
                continue
            environment = entry.get("env", {})
            leaked = set(expect_gco_env) & set(environment if isinstance(environment, dict) else {})
            if leaked:
                problems.append(f"gco-only env leaked onto {name}: {sorted(leaked)}")
    if gco_args is not None and gco_entry.get("args") != gco_args:
        problems.append(f"gco args: expected {gco_args!r}, got {gco_entry.get('args')!r}")

    return problems


def verify_config(
    config: dict[str, Any],
    include_companions: bool = True,
    expect_gco_env: dict[str, str] | None = None,
    gco_args: list[str] | None = None,
) -> list[str]:
    """Return every problem with Claude's generated MCP JSON (empty = valid)."""
    servers = config.get("mcpServers")
    if not isinstance(servers, dict):
        return ["config carries no mcpServers mapping"]
    return _verify_server_mapping(
        servers,
        include_companions=include_companions,
        expect_gco_env=expect_gco_env,
        gco_args=gco_args,
    )


def verify_codex_config(
    config: dict[str, Any],
    include_companions: bool = True,
    expect_gco_env: dict[str, str] | None = None,
    gco_args: list[str] | None = None,
    expected_region: str | None = None,
) -> list[str]:
    """Return every problem with Codex's generated TOML config (empty = valid)."""
    problems: list[str] = []
    expected_model = get_default_codex_model_id()
    if config.get("model") != expected_model:
        problems.append(
            f"Codex model {config.get('model')!r} != shipped default {expected_model!r}"
        )
    if config.get("model_provider") != CODEX_BEDROCK_PROVIDER:
        problems.append(
            f"Codex provider {config.get('model_provider')!r} != {CODEX_BEDROCK_PROVIDER!r}"
        )
    expected_reasoning = get_default_codex_reasoning_effort()
    if config.get("model_reasoning_effort") != expected_reasoning:
        problems.append(
            "Codex reasoning effort "
            f"{config.get('model_reasoning_effort')!r} != {expected_reasoning!r}"
        )
    if config.get("check_for_update_on_startup") is not False:
        problems.append("Codex update checks must be disabled in the generated config")

    providers = config.get("model_providers")
    provider = providers.get(CODEX_BEDROCK_PROVIDER) if isinstance(providers, dict) else None
    if not isinstance(provider, dict) or provider.get("wire_api") != "responses":
        actual_wire_api = provider.get("wire_api") if isinstance(provider, dict) else None
        problems.append(f"Codex wire API {actual_wire_api!r} != 'responses'")
    aws = provider.get("aws") if isinstance(provider, dict) else None
    if not isinstance(aws, dict):
        problems.append(f"Codex config carries no {CODEX_BEDROCK_PROVIDER}.aws provider table")
    else:
        region = aws.get("region")
        if not isinstance(region, str) or not region:
            problems.append(f"Codex provider region must be non-empty, got {region!r}")
        elif expected_region is not None and region != expected_region:
            problems.append(f"Codex provider region {region!r} != expected {expected_region!r}")

    servers = config.get("mcp_servers")
    problems.extend(
        _verify_server_mapping(
            servers,
            include_companions=include_companions,
            expect_gco_env=expect_gco_env,
            gco_args=gco_args,
        )
    )
    if isinstance(servers, dict):
        for name, entry in sorted(servers.items()):
            if isinstance(entry, dict) and entry.get("enabled") is not True:
                problems.append(f"{name}: Codex MCP server must be enabled")
            if (
                isinstance(entry, dict)
                and entry.get("startup_timeout_sec") != CODEX_MCP_STARTUP_TIMEOUT_SECONDS
            ):
                problems.append(
                    f"{name}: Codex MCP startup timeout must be "
                    f"{CODEX_MCP_STARTUP_TIMEOUT_SECONDS} seconds"
                )
    return problems


def _engine_facts(engine: AutopilotEngine) -> tuple[str, str, list[str], str | None]:
    if engine is AutopilotEngine.CODEX:
        return (
            CODEX_VERSION,
            f"{CODEX_PACKAGE}@{CODEX_VERSION}",
            codex_install_command(),
            get_default_codex_reasoning_effort(),
        )
    return (
        CLAUDE_CODE_VERSION,
        f"{CLAUDE_CODE_PACKAGE}@{CLAUDE_CODE_VERSION}",
        claude_install_command(),
        None,
    )


def _default_model(engine: AutopilotEngine) -> str:
    if engine is AutopilotEngine.CODEX:
        return get_default_codex_model_id()
    return get_default_claude_code_model_id()


def verify_plan(
    plan: dict[str, Any],
    claude_binary: str | None = None,
    *,
    engine: str | AutopilotEngine = AutopilotEngine.CLAUDE_CODE,
    codex_binary: str | None = None,
) -> list[str]:
    """Return every problem with a JSON dry-run plan (empty = valid)."""
    resolved_engine = AutopilotEngine(engine)
    problems: list[str] = []
    _version, expected_pin, install_command, expected_reasoning = _engine_facts(resolved_engine)
    expected_model = _default_model(resolved_engine)

    if plan.get("engine") != resolved_engine.value:
        problems.append(f"plan engine {plan.get('engine')!r} != {resolved_engine.value!r}")
    if plan.get("model") != expected_model:
        problems.append(f"plan model {plan.get('model')!r} != shipped default {expected_model!r}")
    if plan.get("reasoning_effort") != expected_reasoning:
        problems.append(
            f"plan reasoning {plan.get('reasoning_effort')!r} != {expected_reasoning!r}"
        )
    if sorted(plan.get("mcp_servers", [])) != expected_servers():
        problems.append(
            f"plan servers {sorted(plan.get('mcp_servers', []))} != {expected_servers()}"
        )
    if plan.get("engine_pin") != expected_pin:
        problems.append(f"plan engine pin {plan.get('engine_pin')!r} != {expected_pin!r}")
    if plan.get("install_command") != " ".join(install_command):
        problems.append(
            "plan install command does not match the selected engine's production command"
        )

    binary_state = codex_binary if resolved_engine is AutopilotEngine.CODEX else claude_binary
    binary = plan.get("engine_binary")
    if binary_state == "absent" and binary is not None:
        problems.append(f"expected no {resolved_engine.value} binary, plan found {binary!r}")
    if binary_state == "present" and not binary:
        problems.append(f"expected an installed {resolved_engine.value} binary, plan detected none")

    selected_pin_field = (
        "codex_pin" if resolved_engine is AutopilotEngine.CODEX else "claude_code_pin"
    )
    selected_binary_field = (
        "codex_binary" if resolved_engine is AutopilotEngine.CODEX else "claude_binary"
    )
    other_pin_field = "claude_code_pin" if resolved_engine is AutopilotEngine.CODEX else "codex_pin"
    other_binary_field = (
        "claude_binary" if resolved_engine is AutopilotEngine.CODEX else "codex_binary"
    )
    if plan.get(selected_pin_field) != expected_pin:
        problems.append(
            f"plan {selected_pin_field} {plan.get(selected_pin_field)!r} != {expected_pin!r}"
        )
    if plan.get(selected_binary_field) != binary:
        problems.append(f"plan {selected_binary_field} disagrees with engine_binary")
    if plan.get(other_pin_field) is not None or plan.get(other_binary_field) is not None:
        problems.append(
            f"plan leaks selected-engine state into {other_pin_field}/{other_binary_field}"
        )

    if resolved_engine is AutopilotEngine.CODEX:
        # The public JSON formatter intentionally omits the large generated
        # config; when an in-process caller supplies it, validate it too.
        rendered = plan.get("codex_config")
        if rendered is not None:
            if not isinstance(rendered, str):
                problems.append("Codex plan config must be TOML text when present")
            else:
                try:
                    codex_config = tomllib.loads(rendered)
                except tomllib.TOMLDecodeError as exc:
                    problems.append(f"Codex plan config is invalid TOML: {exc}")
                else:
                    problems.extend(
                        verify_codex_config(codex_config, expected_region=plan.get("region"))
                    )
    elif plan.get("codex_config") is not None:
        problems.append("Claude plan unexpectedly carries a Codex config")

    return problems


def _load_json(path: str) -> dict[str, Any]:
    with open(path, encoding="utf-8") as handle:
        payload: dict[str, Any] = json.load(handle)
    return payload


def _load_toml(path: str) -> dict[str, Any]:
    with open(path, "rb") as handle:
        return tomllib.load(handle)


def _parse_env_pair(pair: str) -> tuple[str, str]:
    key, separator, value = pair.partition("=")
    if not separator or not key:
        raise argparse.ArgumentTypeError(f"--expect-gco-env expects KEY=VALUE, got {pair!r}")
    return key, value


def _add_engine_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--engine", choices=_ENGINE_CHOICES, default=AutopilotEngine.CLAUDE_CODE.value
    )


def _add_config_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("path")
    parser.add_argument("--no-companions", action="store_true")
    parser.add_argument("--expect-gco-env", action="append", type=_parse_env_pair, default=None)
    parser.add_argument("--gco-args", action="append", default=None)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    for name in ("pin", "install-command", "default-model"):
        _add_engine_argument(sub.add_parser(name))
    sub.add_parser("expected-servers")

    config_parser = sub.add_parser("verify-config")
    _add_config_arguments(config_parser)

    codex_config_parser = sub.add_parser("verify-codex-config")
    _add_config_arguments(codex_config_parser)
    codex_config_parser.add_argument("--region", default=None)

    plan_parser = sub.add_parser("verify-plan")
    plan_parser.add_argument("path")
    _add_engine_argument(plan_parser)
    plan_parser.add_argument("--claude-binary", choices=("present", "absent"), default=None)
    plan_parser.add_argument("--codex-binary", choices=("present", "absent"), default=None)

    args = parser.parse_args(argv)

    if args.command in {"pin", "install-command", "default-model"}:
        engine = AutopilotEngine(args.engine)
        version, _pin, install_command, _reasoning = _engine_facts(engine)
        if args.command == "pin":
            print(version)
        elif args.command == "install-command":
            print(" ".join(install_command))
        else:
            print(_default_model(engine))
        return 0
    if args.command == "expected-servers":
        print("\n".join(expected_servers()))
        return 0

    expect_gco_env = dict(args.expect_gco_env) if getattr(args, "expect_gco_env", None) else None
    if args.command == "verify-config":
        problems = verify_config(
            _load_json(args.path),
            include_companions=not args.no_companions,
            expect_gco_env=expect_gco_env,
            gco_args=args.gco_args,
        )
        label = "Claude config"
    elif args.command == "verify-codex-config":
        problems = verify_codex_config(
            _load_toml(args.path),
            include_companions=not args.no_companions,
            expect_gco_env=expect_gco_env,
            gco_args=args.gco_args,
            expected_region=args.region,
        )
        label = "Codex config"
    else:
        problems = verify_plan(
            _load_json(args.path),
            engine=args.engine,
            claude_binary=args.claude_binary,
            codex_binary=args.codex_binary,
        )
        label = f"{args.engine} plan"

    if problems:
        for problem in problems:
            print(f"ERROR: {problem}", file=sys.stderr)
        return 1
    print(f"autopilot {label} OK ({args.path})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
