"""Documentation-coverage guards for the test suite, CLI, and MCP tools.

Four checks fail CI when reference docs fall behind the code they describe:

* test_every_test_file_is_documented: every tests/test_*.py module is listed in tests/README.md.
* test_every_cli_command_is_documented: every command in the gco Click tree is documented in docs/CLI.md (matched as `gco <path>`).
* test_every_mcp_tool_is_documented: every registered MCP tool (all feature flags on) appears in gco_mcp/tools/README.md.
* test_every_documented_uvx_invocation_pins_the_project_python: every uvx / uv tool install snippet in gco_mcp/README.md pins --python to the minimum version from pyproject's requires-python.

Each test fails with the full list of undocumented items so the fix is mechanical: add the missing entry, with a short description, to the doc. Each guard also asserts a sanity floor on the number of items it discovered, so a broken enumeration (an import error, an empty subprocess result) fails loudly instead of passing vacuously. The MCP catalog is enumerated in a subprocess with GCO_ENABLE_ALL_TOOLS set so the guard sees the full set without perturbing the import-time tool registration the other MCP tests rely on.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
TESTS_DIR = REPO_ROOT / "tests"


def test_every_test_file_is_documented() -> None:
    readme = (TESTS_DIR / "README.md").read_text(encoding="utf-8")
    names = sorted(p.name for p in TESTS_DIR.glob("test_*.py"))
    assert len(names) > 100, f"sanity floor: only discovered {len(names)} test modules"
    missing = [n for n in names if n not in readme]
    assert not missing, (
        "Test modules missing from tests/README.md (add a described row for each):\n "
        + "\n ".join(missing)
    )


def _iter_cli_commands(cmd: object, prefix: str = "") -> list[str]:
    out: list[str] = []
    for name, sub in sorted(getattr(cmd, "commands", {}).items()):
        path = (prefix + " " + name).strip()
        out.append(path)
        out.extend(_iter_cli_commands(sub, path))
    return out


def test_every_cli_command_is_documented() -> None:
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))
    from cli.main import cli

    doc = (REPO_ROOT / "docs" / "CLI.md").read_text(encoding="utf-8")
    commands = _iter_cli_commands(cli)
    assert len(commands) > 50, f"sanity floor: only walked {len(commands)} CLI commands"
    missing = [c for c in commands if not re.search(r"gco " + re.escape(c) + r"(?![\w-])", doc)]
    assert not missing, (
        "CLI commands missing from docs/CLI.md (document each as a `gco <command>` entry):\n "
        + "\n ".join(missing)
    )


_MCP_ENUM = 'import asyncio, json, sys; sys.path.insert(0, "gco_mcp"); import run_mcp; print(json.dumps(sorted(t.name for t in asyncio.run(run_mcp.mcp._list_tools()))))'


def _all_mcp_tool_names() -> list[str]:
    env = dict(os.environ)
    env["GCO_ENABLE_ALL_TOOLS"] = "true"
    proc = subprocess.run(
        [sys.executable, "-c", _MCP_ENUM],
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_every_mcp_tool_is_documented() -> None:
    readme = (REPO_ROOT / "gco_mcp" / "tools" / "README.md").read_text(encoding="utf-8")
    names = _all_mcp_tool_names()
    assert len(names) >= 100, f"sanity floor: only enumerated {len(names)} MCP tools"
    missing = [n for n in names if n not in readme]
    assert not missing, (
        "MCP tools missing from gco_mcp/tools/README.md (add a Tool Reference row for each):\n "
        + "\n ".join(missing)
    )


def _project_minimum_python() -> str:
    """The project's minimum Python (``X.Y``) from pyproject's requires-python.

    Single source of truth for the version every documented ``uvx`` /
    ``uv tool install`` invocation must request: when ``requires-python``
    moves (e.g. to >= 3.15), this guard fails on every stale ``--python``
    value so the docs bump in the same change.
    """
    import tomllib

    with (REPO_ROOT / "pyproject.toml").open("rb") as fh:
        pyproject = tomllib.load(fh)
    requires = pyproject["project"]["requires-python"]
    match = re.fullmatch(r">=\s*(\d+\.\d+)", requires.strip())
    assert match, (
        f"requires-python {requires!r} is not the expected '>=X.Y' shape; "
        "update _project_minimum_python() alongside it"
    )
    return match.group(1)


def test_every_documented_uvx_invocation_pins_the_project_python() -> None:
    """uvx / uv tool install snippets pin --python to the project's version.

    ``uvx`` resolves against the host's default interpreter unless told
    otherwise, so on machines whose default Python is older than the
    project's ``requires-python`` a documented invocation without
    ``--python`` fails resolution with ``No solution found … does not
    satisfy Python>=3.14``. With ``--python <min>`` uv selects a matching
    interpreter and auto-downloads a managed CPython when the host has
    none, so the snippet works on any machine.

    The guard checks both presence and value: every ``--python`` in
    gco_mcp/README.md's uvx JSON configs and shell snippets must equal the
    minimum version from pyproject's ``requires-python``, so a future
    Python bump cannot leave the documented commands requesting a stale
    interpreter. Failures list each offender; the fix is mechanical.
    """
    expected = _project_minimum_python()
    readme = (REPO_ROOT / "gco_mcp" / "README.md").read_text(encoding="utf-8")
    offenders: list[str] = []

    # JSON config blocks: every mcpServers entry that launches THIS package's
    # gco-mcp via uvx must carry --python immediately followed by the
    # project's minimum version. Entries for third-party servers documented
    # alongside (aws-docs, fetch, …) have their own Python requirements and
    # are out of scope.
    json_blocks = re.findall(r"```json\n(.*?)```", readme, flags=re.DOTALL)
    uvx_json_entries = 0
    for block in json_blocks:
        try:
            payload = json.loads(block)
        except json.JSONDecodeError:
            continue
        for name, server in (payload.get("mcpServers") or {}).items():
            args_blob = json.dumps(server.get("args") or [])
            if server.get("command") != "uvx" or "gco-mcp" not in args_blob:
                continue
            uvx_json_entries += 1
            args = list(server.get("args") or [])
            if "--python" not in args:
                offenders.append(f"mcpServers[{name!r}] json block missing --python in args")
                continue
            value_index = args.index("--python") + 1
            value = args[value_index] if value_index < len(args) else "<missing>"
            if value != expected:
                offenders.append(
                    f"mcpServers[{name!r}] json block pins --python {value!r}, "
                    f"expected {expected!r}"
                )

    # Shell snippets: any uvx run or uv tool install of THIS package must
    # carry --python <expected> on the same line. Scoped to lines that
    # reference the gco-mcp script or the GCO repository so documented
    # third-party uvx commands stay out of scope.
    uvx_shell_lines = 0
    for line in readme.splitlines():
        stripped = line.strip()
        is_ours = "gco-mcp" in stripped or "global-capacity-orchestrator-on-aws" in stripped
        is_uvx_run = "uvx " in stripped and "--from" in stripped
        is_tool_install = stripped.startswith("uv tool install")
        if not is_ours or not (is_uvx_run or is_tool_install):
            continue
        uvx_shell_lines += 1
        match = re.search(r"--python\s+(\S+)", stripped)
        if match is None:
            offenders.append(f"shell snippet missing --python: {stripped}")
        elif match.group(1) != expected:
            offenders.append(
                f"shell snippet pins --python {match.group(1)!r}, expected {expected!r}: {stripped}"
            )

    # Sanity floors so a broken enumeration fails loudly instead of passing
    # vacuously (same pattern as the other guards in this module).
    assert uvx_json_entries >= 5, (
        f"sanity floor: only discovered {uvx_json_entries} uvx JSON config entries"
    )
    assert uvx_shell_lines >= 2, (
        f"sanity floor: only discovered {uvx_shell_lines} uvx/uv-tool shell snippets"
    )
    assert not offenders, (
        "Documented uvx invocations out of sync with requires-python "
        f"(every one must pin --python {expected}):\n  " + "\n  ".join(offenders)
    )
