"""
CLI runner for the GCO MCP server.

Provides synchronous and cancellation-aware asynchronous wrappers which shell
out to the ``gco`` CLI with ``--output json`` and return the result. All
arguments are passed as separate list elements (shell=False) to prevent command
injection.
"""

import asyncio
import json
import os
import shutil
import subprocess
import sys
from contextlib import suppress
from pathlib import Path


def _resolve_project_root() -> Path:
    """Resolve the directory every ``gco`` subprocess runs from.

    The CLI discovers ``cdk.json`` (and the rest of the checkout) by walking
    up from its working directory, so this choice decides whether config- and
    stack-aware tools see the user's project. Resolution order:

    1. ``GCO_PROJECT_ROOT`` environment variable — explicit override for MCP
       clients that cannot set a server working directory. Ignored (with a
       stderr warning) when it does not point at an existing directory.
    2. The package's parent directory, when it is a checkout (has
       ``cdk.json``) — the clone / editable-install / dev-container layout,
       where the historical ``Path(__file__).parent.parent`` was correct.
    3. The nearest ancestor of the process working directory containing
       ``cdk.json`` — a ``uvx`` / ``uv tool install`` launched with the MCP
       client's ``cwd`` pointing at (or inside) a checkout. Previously this
       layout silently pinned subprocesses to uv's site-packages and the
       client-provided ``cwd`` never reached the CLI.
    4. The process working directory itself — matches the CLI's own
       fallback when no ``cdk.json`` is found (AWS-facing tools need none).
    """
    env_root = os.environ.get("GCO_PROJECT_ROOT", "").strip()
    if env_root:
        candidate = Path(env_root).expanduser()
        if candidate.is_dir():
            return candidate.resolve()
        print(
            f"gco-mcp: GCO_PROJECT_ROOT={env_root!r} is not a directory; ignoring it.",
            file=sys.stderr,
        )
    package_parent = Path(__file__).resolve().parent.parent
    if (package_parent / "cdk.json").exists():
        return package_parent
    cwd = Path.cwd()
    for parent in (cwd, *cwd.parents):
        if (parent / "cdk.json").exists():
            return parent
    return cwd


PROJECT_ROOT = _resolve_project_root()


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


def _run_cli(
    *args: str,
    timeout_seconds: int = 120,
    pass_fds: tuple[int, ...] = (),
) -> str:
    """Run a gco CLI command and return its output.

    All args are passed as separate list elements to subprocess (shell=False),
    so shell metacharacters in user-provided values are treated as literals
    and cannot cause command injection. Path arguments are validated to prevent
    traversal outside the project root. ``timeout_seconds`` may be increased by
    wrappers for intentionally long-running transfers while preserving the
    two-minute default for normal tools. ``pass_fds`` is reserved for verified
    descriptor-backed local-data paths and is omitted from ``subprocess.run``
    when empty for compatibility with platforms that do not support it.
    """
    # Validate any path-like arguments to prevent directory traversal.
    for arg in args:
        if arg.startswith("-"):
            continue  # flag, not a path
        if ".." in arg.split("/"):
            return json.dumps({"error": f"Invalid argument: path traversal not allowed: {arg}"})

    cmd = [_gco_executable(), "--output", "json", *args]
    try:
        if pass_fds:
            result = subprocess.run(  # nosemgrep: dangerous-subprocess-use-audit - shell=False; validated literal argv
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
                cwd=str(PROJECT_ROOT),
                pass_fds=pass_fds,
            )
        else:
            result = subprocess.run(  # nosemgrep: dangerous-subprocess-use-audit - shell=False; validated literal argv
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
                cwd=str(PROJECT_ROOT),
            )
        output = result.stdout.strip()
        if result.returncode != 0:
            error = result.stderr.strip() or output
            return json.dumps({"error": error, "exit_code": result.returncode})
        return output if output else json.dumps({"status": "ok"})
    except subprocess.TimeoutExpired:
        return json.dumps({"error": f"Command timed out after {timeout_seconds} seconds"})
    except FileNotFoundError:
        return json.dumps(
            {
                "error": "gco CLI not found. Install GCO so the gco console script is on PATH (e.g. uv tool install the GCO git URL, or pip install -e . from a clone)."
            }
        )


async def _stop_cli_process(
    process: asyncio.subprocess.Process,
    communication: asyncio.Task[tuple[bytes, bytes]],
    *,
    grace_seconds: float,
) -> None:
    """Terminate a CLI process and drain output before escalating to a kill."""
    if process.returncode is None:
        with suppress(ProcessLookupError):
            process.terminate()
    try:
        await asyncio.wait_for(asyncio.shield(communication), timeout=grace_seconds)
    except TimeoutError:
        if process.returncode is None:
            with suppress(ProcessLookupError):
                process.kill()
        await communication


async def _run_cli_async(
    *args: str,
    timeout_seconds: int = 120,
    terminate_grace_seconds: float = 5,
) -> str:
    """Run ``gco`` asynchronously and terminate it on timeout or cancellation."""
    for arg in args:
        if arg.startswith("-"):
            continue
        if ".." in arg.split("/"):
            return json.dumps({"error": f"Invalid argument: path traversal not allowed: {arg}"})

    cmd = [_gco_executable(), "--output", "json", *args]
    try:
        process = await asyncio.create_subprocess_exec(  # nosemgrep: dangerous-subprocess-use-audit - shell=False; args are validated and passed as literal argv elements
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(PROJECT_ROOT),
        )
    except FileNotFoundError:
        return json.dumps(
            {
                "error": "gco CLI not found. Install GCO so the gco console script is on PATH (e.g. uv tool install the GCO git URL, or pip install -e . from a clone)."
            }
        )

    communication = asyncio.create_task(process.communicate())
    try:
        stdout_bytes, stderr_bytes = await asyncio.wait_for(
            asyncio.shield(communication), timeout=timeout_seconds
        )
    except TimeoutError:
        await _stop_cli_process(
            process,
            communication,
            grace_seconds=terminate_grace_seconds,
        )
        return json.dumps({"error": f"Command timed out after {timeout_seconds} seconds"})
    except asyncio.CancelledError:
        await _stop_cli_process(
            process,
            communication,
            grace_seconds=terminate_grace_seconds,
        )
        raise

    output = stdout_bytes.decode(errors="replace").strip()
    if process.returncode != 0:
        error = stderr_bytes.decode(errors="replace").strip() or output
        return json.dumps({"error": error, "exit_code": process.returncode})
    return output if output else json.dumps({"status": "ok"})
