"""Contract guard: every MCP tool builds a valid ``gco`` CLI invocation.

Most MCP tools shell out via ``cli_runner._run_cli("group", "cmd", "-flag", ...)``.
The existing MCP tests assert the argv a tool *constructs*, but nothing checked
that argv against the real Click command tree — so a tool could emit a
subcommand or flag the CLI rejects and every test stayed green. That's exactly
how ``nodepools_create_odcr`` drifted: it passed a positional ``name`` plus
``--count`` / ``--cluster`` / ``--taint`` / ``--label``, none of which the
``gco nodepools create-odcr`` command accepts.

This test closes that gap. A subprocess (with ``GCO_ENABLE_ALL_TOOLS`` so the
flag-gated tools are registered too) invokes each tool with generated dummy
arguments, patching ``cli_runner._run_cli`` to capture the argv instead of
running it. The parent process then resolves each captured argv against the
live ``gco`` Click tree and asserts the command path exists and every ``--flag``
is a real option of the resolved command. Tools that don't shell out to the CLI
(they call a different backend) never invoke ``_run_cli`` and are skipped.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import click

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Runs in a subprocess so import-time, flag-gated tool registration doesn't
# perturb the in-process FastMCP singleton the other MCP tests rely on. It
# invokes every registered tool with dummy args (booleans tried both ways so
# `--no-*` style flags are exercised) and prints {tool_name: [argv, ...]} —
# the argv lists captured from cli_runner._run_cli. Tools that never call
# _run_cli (non-CLI backends) or raise on dummy args yield an empty list.
_DUMP_SNIPPET = r"""
import asyncio, inspect, json, sys

sys.path.insert(0, "gco_mcp")
import cli_runner
import run_mcp

_captured = []


def _fake_run_cli(*args):
    _captured.append([str(a) for a in args])
    return "{}"


cli_runner._run_cli = _fake_run_cli


def _shells_out(fn):
    # Only tools whose body calls cli_runner._run_cli are safe to invoke here —
    # invoking a non-CLI backend tool (mission_*, metrics_*, inference/session
    # backends) could do real work, hang, or write to disk. audit_logged uses
    # functools.wraps, so unwrap reaches the original body for source inspection.
    candidates = {fn}
    try:
        candidates.add(inspect.unwrap(fn))
    except Exception:
        pass
    for candidate in candidates:
        try:
            if "_run_cli" in inspect.getsource(candidate):
                return True
        except (OSError, TypeError):
            continue
    return False


def _dummy(annotation, bool_value):
    text = str(annotation).lower()
    if "list" in text or "sequence" in text or "tuple" in text:
        return ["x"]
    if "dict" in text or "mapping" in text:
        return {"x": "y"}
    if "bool" in text:
        return bool_value
    if "float" in text:
        return 1.0
    if "int" in text:
        return 1
    return "x"


def _kwargs(fn, bool_value):
    out = {}
    for name, param in inspect.signature(fn).parameters.items():
        if param.kind in (param.VAR_POSITIONAL, param.VAR_KEYWORD):
            continue
        out[name] = _dummy(param.annotation, bool_value)
    return out


def _invoke(fn, bool_value):
    global _captured
    _captured = []
    try:
        kwargs = _kwargs(fn, bool_value)
        result = fn(**kwargs)
        if inspect.iscoroutine(result):
            asyncio.run(result)
    except Exception:
        return None
    return list(_captured)


tools = asyncio.run(run_mcp.mcp._list_tools())
result = {}
for tool in tools:
    fn = getattr(run_mcp, tool.name, None)
    if fn is None or not callable(fn) or not _shells_out(fn):
        result[tool.name] = []
        continue
    argvs = []
    for bv in (True, False):
        captured = _invoke(fn, bv)
        if captured:
            argvs.extend(captured)
    # de-dupe identical argvs
    seen = set()
    uniq = []
    for argv in argvs:
        key = tuple(argv)
        if key not in seen:
            seen.add(key)
            uniq.append(argv)
    result[tool.name] = uniq

print(json.dumps(result))
"""


def _dump_tool_argvs() -> dict[str, list[list[str]]]:
    env = dict(os.environ)
    env["GCO_ENABLE_ALL_TOOLS"] = "true"
    proc = subprocess.run(
        [sys.executable, "-c", _DUMP_SNIPPET],
        cwd=str(PROJECT_ROOT),
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(proc.stdout.strip().splitlines()[-1])


def _valid_option_strings(command: click.Command) -> set[str]:
    opts = {"--help", "-h"}
    for param in command.params:
        if isinstance(param, click.Option):
            opts.update(param.opts)
            opts.update(param.secondary_opts)
    return opts


def _validate_argv(root: click.Command, argv: list[str]) -> list[str]:
    """Resolve argv against the Click tree; return a list of problems (empty = ok)."""
    node: click.Command = root
    i = 0
    while i < len(argv):
        token = argv[i]
        if token.startswith("-"):
            break
        sub = getattr(node, "commands", {}).get(token)
        if sub is None:
            break
        node = sub
        i += 1

    path = " ".join(argv[:i]) or "<root>"
    remaining = argv[i:]
    problems: list[str] = []

    # A group needs a valid subcommand next; a non-flag token here is a typo'd
    # subcommand (e.g. "capacity reservaton-check").
    if remaining and not remaining[0].startswith("-") and isinstance(node, click.Group):
        problems.append(f"unknown subcommand {remaining[0]!r} under 'gco {path}'")

    valid = _valid_option_strings(node)
    for token in remaining:
        if not token.startswith("-"):
            continue  # option value or positional argument — not validated here
        name = token.split("=", 1)[0]  # tolerate --opt=value
        if name not in valid:
            problems.append(f"unknown option {name!r} for 'gco {path}'")
    return problems


def test_every_mcp_tool_builds_a_valid_cli_invocation() -> None:
    from cli.main import cli

    tool_argvs = _dump_tool_argvs()
    assert tool_argvs, "subprocess returned no tools"

    shell_out_tools = {name: argvs for name, argvs in tool_argvs.items() if argvs}
    # Sanity floor: the bulk of the catalog shells out to the CLI, so if almost
    # nothing was captured the harness is broken and the guard is vacuous.
    assert len(shell_out_tools) >= 40, (
        f"only captured argv for {len(shell_out_tools)} tools — harness likely broken"
    )

    violations: list[str] = []
    for name, argvs in shell_out_tools.items():
        for argv in argvs:
            for problem in _validate_argv(cli, argv):
                violations.append(f"{name}: {problem}  (argv={argv})")

    assert not violations, (
        "MCP tools build CLI invocations the gco Click tree rejects:\n  " + "\n  ".join(violations)
    )
