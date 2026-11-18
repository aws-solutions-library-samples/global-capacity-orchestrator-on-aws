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
from collections.abc import Mapping
from contextlib import suppress
from pathlib import Path
from typing import Any


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


class _NotJson(Exception):
    """Raised by :func:`_strict_json` for anything but one standard JSON value."""


def _strict_json(text: str) -> Any:
    """Parse ``text`` as exactly one standard JSON value (no NaN/Infinity)."""

    def reject_nonstandard_constant(token: str) -> None:
        raise ValueError(f"non-standard JSON constant: {token}")

    try:
        return json.loads(text, parse_constant=reject_nonstandard_constant)
    except json.JSONDecodeError, ValueError:
        raise _NotJson from None


def _validated_cli_json_output(output: str) -> str:
    """Return one strict JSON value or a fail-closed MCP error envelope."""
    if not output:
        return json.dumps(
            {
                "error": "gco CLI returned empty stdout despite --output json",
                "exit_code": 1,
            }
        )
    try:
        _strict_json(output)
    except _NotJson:
        return json.dumps(
            {
                "error": "gco CLI returned malformed or multiple JSON documents",
                "exit_code": 1,
            }
        )
    return output


def _failure_envelope(stdout: str, stderr: str, exit_code: int) -> str:
    """Return the error envelope for a ``gco`` run that exited non-zero.

    ``error`` is stderr, else stdout. Several status commands report failing
    checks as data: they print one JSON document (add-on rows, capability or
    HyperPod status, a preflight or doctor summary) and exit 1. When stdout
    is such a document it is also returned, parsed, under ``output``, so the
    caller gets the structured result instead of JSON text inside ``error``;
    with stderr empty, ``error`` then names the exit status.
    """
    try:
        document = _strict_json(stdout)
    except _NotJson:
        return json.dumps({"error": stderr or stdout, "exit_code": exit_code})
    return json.dumps(
        {
            "error": stderr or f"gco exited with status {exit_code}",
            "exit_code": exit_code,
            "output": document,
        }
    )


def reject_paths_outside_project_root(**paths: str | None) -> str | None:
    """Return an error envelope if a local path argument escapes the project root.

    Every ``gco`` subprocess runs from :data:`PROJECT_ROOT`, and the tools'
    local path arguments (manifests, batch scripts, build contexts, CDK
    outputs files) are meant to name files in that project. Each value is
    resolved the way the CLI will open it: a relative path against the
    project root, ``~`` not expanded, and symlinks followed. A value that
    resolves outside the root, whether absolute, through ``..`` or through a
    symlink, is rejected. ``None`` or empty values are skipped. Only path
    parameters belong here: prompts, JSON payloads, and other free text are
    passed to the CLI unchanged.

    Returns ``None`` when every path is confined, otherwise a JSON error
    envelope with ``code: path_outside_project_root`` naming the parameter.
    """
    root = Path(os.path.realpath(PROJECT_ROOT))
    for name, value in paths.items():
        if not value:
            continue
        candidate = Path(value)
        if not candidate.is_absolute():
            candidate = root / candidate
        try:
            resolved: Path | None = Path(os.path.realpath(candidate))
        except OSError, ValueError:
            resolved = None  # e.g. an embedded NUL byte: not a usable path
        if resolved is None or not resolved.is_relative_to(root):
            return json.dumps(
                {
                    "error": f"{name} must be inside the project root {root}: {value}",
                    "code": "path_outside_project_root",
                    "parameter": name,
                }
            )
    return None


def reject_traversal_segments(**values: str | None) -> str | None:
    """Return an error envelope if a value bound for an API request path has a ``..`` segment.

    Several ``gco`` commands put a name or ID straight into the path of a GCO
    API request (``/api/v1/jobs/{namespace}/{name}``, templates, webhooks,
    queue and Slurm job IDs) and ``gco inference invoke --path`` appends its
    value to the endpoint's path. The HTTP client removes ``..`` segments
    before sending, and SigV4 signs the normalized path, so such a value would
    reach a different API route than the one the tool names. Values with a
    ``..`` segment (split on ``/`` or ``\\``) are rejected; ``None`` or empty
    values are skipped.

    Returns ``None`` when every value is clean, otherwise a JSON error
    envelope with ``code: path_traversal_segment`` naming the parameter.
    """
    for name, value in values.items():
        if value and ".." in value.replace("\\", "/").split("/"):
            return json.dumps(
                {
                    "error": f"{name} must not contain a '..' path segment: {value}",
                    "code": "path_traversal_segment",
                    "parameter": name,
                }
            )
    return None


def _run_cli(
    *args: str,
    timeout_seconds: int = 120,
    pass_fds: tuple[int, ...] = (),
    env: Mapping[str, str] | None = None,
) -> str:
    """Run a gco CLI command and return its output.

    All args are passed as separate list elements to subprocess (shell=False),
    so shell metacharacters in user-provided values are treated as literals
    and cannot cause command injection. Tools check their own path arguments
    before calling this: local paths with
    :func:`reject_paths_outside_project_root`, and names or IDs the CLI puts
    into an API request path with :func:`reject_traversal_segments`. Other
    arguments, such as prompts and JSON bodies, are passed through unchanged.

    ``timeout_seconds`` may be increased by wrappers for intentionally
    long-running transfers while preserving the two-minute default for
    normal tools. ``pass_fds`` is reserved for verified descriptor-backed
    local-data paths and is omitted from ``subprocess.run`` when empty for
    compatibility with platforms that do not support it.

    ``env`` adds variables to the child's environment on top of the
    server's own. Tools use it for secrets that a CLI option also reads
    from an environment variable (passwords, webhook secrets), because a
    process's argv is visible to every local user. The values are never
    logged; without ``env`` the child inherits the environment unchanged.
    """
    cmd = [_gco_executable(), "--output", "json", *args]
    child_env = {**os.environ, **env} if env else None
    try:
        if pass_fds:
            result = subprocess.run(  # nosemgrep: dangerous-subprocess-use-audit - shell=False; validated literal argv
                cmd,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
                cwd=str(PROJECT_ROOT),
                env=child_env,
                pass_fds=pass_fds,
                check=False,
            )
        else:
            result = subprocess.run(  # nosemgrep: dangerous-subprocess-use-audit - shell=False; validated literal argv
                cmd,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
                cwd=str(PROJECT_ROOT),
                env=child_env,
                check=False,
            )
        output = result.stdout.strip()
        if result.returncode != 0:
            return _failure_envelope(output, result.stderr.strip(), result.returncode)
        return _validated_cli_json_output(output)
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
    """Run ``gco`` asynchronously and terminate it on timeout or cancellation.

    Like :func:`_run_cli`, arguments are passed through unchanged; callers
    check their own path arguments first.
    """
    cmd = [_gco_executable(), "--output", "json", *args]
    try:
        process = await asyncio.create_subprocess_exec(  # nosemgrep: dangerous-subprocess-use-audit - shell=False; args are validated and passed as literal argv elements
            *cmd,
            stdin=asyncio.subprocess.DEVNULL,
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
        stderr = stderr_bytes.decode(errors="replace").strip()
        return _failure_envelope(output, stderr, process.returncode or 1)
    return _validated_cli_json_output(output)
