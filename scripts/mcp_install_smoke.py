"""Smoke-test that an installed GCO exposes a working, self-contained gco-mcp.

Run with the target environment interpreter:

    <venv>/bin/python scripts/mcp_install_smoke.py

Asserts the package imports from site-packages (not a working-tree checkout),
the PyPI mcp SDK is not shadowed by gco_mcp, main() is callable, and the server
resolves its OWN bundled gco CLI rather than an unrelated one earlier on PATH.
"""

import sys
from pathlib import Path

import mcp

import gco_mcp
import gco_mcp.cli_runner as cli_runner
import gco_mcp.run_mcp as run_mcp

bindir = str(Path(sys.executable).parent)
pkg = gco_mcp.__file__ or (list(gco_mcp.__path__)[0] if gco_mcp.__path__ else "")
mcp_file = mcp.__file__ or ""
gco_exe = cli_runner._gco_executable()

problems = []
if "site-packages" not in str(pkg):
    problems.append("gco_mcp not imported from site-packages: " + str(pkg))
if not callable(getattr(run_mcp, "main", None)):
    problems.append("gco_mcp.run_mcp.main is not callable")
if "site-packages/mcp/" not in mcp_file:
    problems.append("mcp SDK shadowed by gco_mcp: " + mcp_file)
if not gco_exe.startswith(bindir):
    problems.append("server resolved a non-bundled gco: " + gco_exe)

if problems:
    print("MCP install smoke test FAILED:")
    for p in problems:
        print("- " + p)
    sys.exit(1)

print("MCP install smoke test OK")
print("gco_mcp: " + str(pkg))
print("mcp SDK: " + str(mcp.__file__))
print("bundled gco: " + gco_exe)
