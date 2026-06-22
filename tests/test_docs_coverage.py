"""Documentation-coverage guards for the test suite, CLI, and MCP tools.

Three checks fail CI when reference docs fall behind the code they describe:

* test_every_test_file_is_documented: every tests/test_*.py module is listed in tests/README.md.
* test_every_cli_command_is_documented: every command in the gco Click tree is documented in docs/CLI.md (matched as `gco <path>`).
* test_every_mcp_tool_is_documented: every registered MCP tool (all feature flags on) appears in gco_mcp/tools/README.md.

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
