"""Single source for the autopilot facts and assertions CI jobs share.

Three jobs exercise ``gco autopilot`` (unit:cli:autopilot, the dev-container
matrix step, integration:autopilot:boot) and they used to carry their own
inline copies of the same checks — expected MCP server set, config shape,
pruned-package bans, default-model equality. This module is the one place
those assertions live; every fact is *derived* from the production modules
(``cli.autopilot``, ``gco.bedrock``), so there is no literal here to bump.

Subcommands (all print to stdout; verifiers exit 1 listing every problem):

    pin                         the pinned Claude Code version
    install-command             the exact npm install command autopilot runs
    default-model               the shipped Bedrock default model id
    expected-servers            expected MCP server names, one per line
    verify-config PATH          validate a --print-config JSON document
        [--no-companions]         expect only the gco server
        [--expect-gco-env K=V]    require an env pair on the gco entry (repeatable)
        [--gco-args ARG]          require the gco entry args to equal exactly
                                  these values (repeatable, in order)
    verify-plan PATH            validate a -o json --dry-run plan document
        [--claude-binary present|absent]

Importable for pytest: ``expected_servers()``, ``verify_config()``,
``verify_plan()``, ``main()``.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from cli.autopilot import (  # noqa: E402
    CLAUDE_CODE_PACKAGE,
    CLAUDE_CODE_VERSION,
    COMPANION_MCP_SERVERS,
    claude_install_command,
)
from gco.bedrock import get_default_claude_code_model_id  # noqa: E402

#: Companions deliberately pruned from the curated registry; their names
#: must never reappear anywhere in a generated session config.
PRUNED_PACKAGES: tuple[str, ...] = ("mcp-server-fetch", "mcp-server-calculator")


def expected_servers(include_companions: bool = True) -> list[str]:
    """The exact MCP server set a generated session config must carry."""
    names = {"gco"}
    if include_companions:
        names |= {companion.name for companion in COMPANION_MCP_SERVERS}
    return sorted(names)


def verify_config(
    config: dict,
    include_companions: bool = True,
    expect_gco_env: dict[str, str] | None = None,
    gco_args: list[str] | None = None,
) -> list[str]:
    """Return every problem with a generated MCP config (empty = valid)."""
    problems: list[str] = []
    servers = config.get("mcpServers")
    if not isinstance(servers, dict):
        return ["config carries no mcpServers mapping"]

    expected = expected_servers(include_companions)
    actual = sorted(servers)
    if actual != expected:
        missing = sorted(set(expected) - set(actual))
        extra = sorted(set(actual) - set(expected))
        problems.append(f"server set mismatch: missing={missing} unexpected={extra}")

    for name, entry in sorted(servers.items()):
        command = entry.get("command")
        if not isinstance(command, str) or not command:
            problems.append(f"{name}: command must be a non-empty string, got {command!r}")
        args = entry.get("args")
        if not isinstance(args, list) or not all(isinstance(a, str) for a in args):
            problems.append(f"{name}: args must be a list of strings, got {args!r}")

    text = json.dumps(config)
    for pruned in PRUNED_PACKAGES:
        if pruned in text:
            problems.append(f"pruned package {pruned!r} reappeared in the generated config")

    gco_entry = servers.get("gco", {})
    if expect_gco_env is not None:
        gco_env = gco_entry.get("env", {})
        for key, value in sorted(expect_gco_env.items()):
            if gco_env.get(key) != value:
                problems.append(f"gco env {key!r}: expected {value!r}, got {gco_env.get(key)!r}")
        for name, entry in sorted(servers.items()):
            if name == "gco":
                continue
            leaked = set(expect_gco_env) & set(entry.get("env", {}))
            if leaked:
                problems.append(f"gco-only env leaked onto {name}: {sorted(leaked)}")
    if gco_args is not None and gco_entry.get("args") != gco_args:
        problems.append(f"gco args: expected {gco_args!r}, got {gco_entry.get('args')!r}")

    return problems


def verify_plan(plan: dict, claude_binary: str | None = None) -> list[str]:
    """Return every problem with a ``-o json --dry-run`` plan (empty = valid)."""
    problems: list[str] = []

    expected_model = get_default_claude_code_model_id()
    if plan.get("model") != expected_model:
        problems.append(f"plan model {plan.get('model')!r} != shipped default {expected_model!r}")

    if sorted(plan.get("mcp_servers", [])) != expected_servers():
        problems.append(
            f"plan servers {sorted(plan.get('mcp_servers', []))} != {expected_servers()}"
        )

    pin = f"{CLAUDE_CODE_PACKAGE}@{CLAUDE_CODE_VERSION}"
    if plan.get("claude_code_pin") != pin:
        problems.append(f"plan pin {plan.get('claude_code_pin')!r} != {pin!r}")

    if claude_binary == "absent" and plan.get("claude_binary") is not None:
        problems.append(f"expected no claude binary, plan found {plan.get('claude_binary')!r}")
    if claude_binary == "present" and not plan.get("claude_binary"):
        problems.append("expected an installed claude binary, plan detected none")

    return problems


def _load(path: str) -> dict:
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def _parse_env_pair(pair: str) -> tuple[str, str]:
    key, separator, value = pair.partition("=")
    if not separator or not key:
        raise argparse.ArgumentTypeError(f"--expect-gco-env expects KEY=VALUE, got {pair!r}")
    return key, value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("pin")
    sub.add_parser("install-command")
    sub.add_parser("default-model")
    sub.add_parser("expected-servers")

    config_parser = sub.add_parser("verify-config")
    config_parser.add_argument("path")
    config_parser.add_argument("--no-companions", action="store_true")
    config_parser.add_argument(
        "--expect-gco-env", action="append", type=_parse_env_pair, default=None
    )
    config_parser.add_argument("--gco-args", action="append", default=None)

    plan_parser = sub.add_parser("verify-plan")
    plan_parser.add_argument("path")
    plan_parser.add_argument("--claude-binary", choices=("present", "absent"), default=None)

    args = parser.parse_args(argv)

    if args.command == "pin":
        print(CLAUDE_CODE_VERSION)
        return 0
    if args.command == "install-command":
        print(" ".join(claude_install_command()))
        return 0
    if args.command == "default-model":
        print(get_default_claude_code_model_id())
        return 0
    if args.command == "expected-servers":
        print("\n".join(expected_servers()))
        return 0

    if args.command == "verify-config":
        problems = verify_config(
            _load(args.path),
            include_companions=not args.no_companions,
            expect_gco_env=dict(args.expect_gco_env) if args.expect_gco_env else None,
            gco_args=args.gco_args,
        )
        label = "config"
    else:
        problems = verify_plan(_load(args.path), claude_binary=args.claude_binary)
        label = "plan"

    if problems:
        for problem in problems:
            print(f"✗ {problem}", file=sys.stderr)
        return 1
    print(f"autopilot {label} OK ({args.path})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
