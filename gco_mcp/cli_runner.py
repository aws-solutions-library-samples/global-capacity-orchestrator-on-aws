"""
CLI runner for the GCO MCP server.

Provides ``_run_cli()`` which shells out to the ``gco`` CLI with
``--output json`` and returns the result. All arguments are passed as
separate list elements (shell=False) to prevent command injection.
"""

import json
import shutil
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent


def _gco_executable() -> str:
    """Resolve the ``gco`` CLI to invoke.

    Prefer the ``gco`` console script installed next to the current
    interpreter -- the copy shipped in the SAME environment as this MCP
    server -- so a ``uv tool install`` / ``uvx`` install is self-contained
    and version-matched, never picking up an unrelated ``gco`` earlier on
    PATH. Fall back to a PATH lookup (the dev / pipx layout), then the bare
    name so the FileNotFoundError handler below can report it.
    """
    bindir = Path(sys.executable).parent
    for name in ("gco", "gco.exe"):
        candidate = bindir / name
        if candidate.exists():
            return str(candidate)
    return shutil.which("gco") or "gco"


def _run_cli(*args: str) -> str:
    """Run a gco CLI command and return its output.

    All args are passed as separate list elements to subprocess (shell=False),
    so shell metacharacters in user-provided values are treated as literals
    and cannot cause command injection. Path arguments are validated to prevent
    traversal outside the project root.
    """
    # Validate any path-like arguments to prevent directory traversal.
    for arg in args:
        if arg.startswith("-"):
            continue  # flag, not a path
        if ".." in arg.split("/"):
            return json.dumps({"error": f"Invalid argument: path traversal not allowed: {arg}"})

    cmd = [_gco_executable(), "--output", "json", *args]
    try:
        result = subprocess.run(  # nosemgrep: dangerous-subprocess-use-audit - shell=False; args are validated above and passed as literal argv elements
            cmd,
            capture_output=True,
            text=True,
            timeout=120,
            cwd=str(PROJECT_ROOT),
        )
        output = result.stdout.strip()
        if result.returncode != 0:
            error = result.stderr.strip() or output
            return json.dumps({"error": error, "exit_code": result.returncode})
        return output if output else json.dumps({"status": "ok"})
    except subprocess.TimeoutExpired:
        return json.dumps({"error": "Command timed out after 120 seconds"})
    except FileNotFoundError:
        return json.dumps(
            {
                "error": "gco CLI not found. Install GCO so the gco console script is on PATH (e.g. uv tool install the GCO git URL, or pip install -e . from a clone)."
            }
        )
