"""Run the dashboard in a container of its own: build it, start it, stop it, read it.

``gco dashboard start`` on a workstation builds ``Dockerfile.dashboard`` from
the GCO checkout and runs the image detached, so the dashboard keeps running
after the terminal closes. The container gets only what the dashboard needs:

* the port is published on the host's loopback address alone;
* ``~/.aws`` and the checkout (for ``cdk.json`` and ``examples/``) are
  mounted read-only, and the root filesystem is read-only too;
* every capability is dropped and privilege escalation is off;
* it runs as the calling user's uid, so it can read ``~/.aws`` files that
  are private to that user;
* the access token travels in the environment, never on a command line.

The server inside is told the host port its port is published on, so the
address it prints in the container's log names the port a browser uses.

The image is rebuilt only when its sources change: it carries a label with a
fingerprint of every build input, and a start compares it with the
checkout's. Where the dashboard runs, and with which token, is kept in
``~/.gco/dashboard.json`` (owner-only) for ``gco dashboard open``,
``status`` and ``stop``.
"""

from __future__ import annotations

import contextlib
import dataclasses
import hashlib
import json
import os
import subprocess
import sys
import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from cli.dashboard.server import (
    BROWSER_PORT_ENV,
    DEFAULT_PORT,
    TOKEN_ENV,
    dashboard_url,
    healthy,
)

#: The image and container name (one dashboard per workstation).
NAME = "gco-dashboard"
IMAGE_TAG = "local"
DOCKERFILE = "Dockerfile.dashboard"
#: The port the server listens on inside the container.
CONTAINER_PORT = DEFAULT_PORT
#: Image label holding the fingerprint of the sources it was built from.
SOURCE_LABEL = "io.gco.dashboard.source"
#: Label every dashboard container carries.
CONTAINER_LABEL = "io.gco.dashboard"
#: The runtime user's home in the image (``dockerfiles/build_scratch_rootfs.py``).
CONTAINER_HOME = "/home/gco"
#: Where the checkout is mounted, and the server's working directory.
CONTAINER_WORKSPACE = "/workspace"
#: The container's scratch space, a tmpfs over its read-only root filesystem.
CONTAINER_TMP = "/tmp"  # nosec B108  # a tmpfs inside the container, not a host directory
#: Overrides where the state file lives (tests use it).
STATE_ENV = "GCO_DASHBOARD_STATE"

#: Files and trees the image is built from; their contents make the fingerprint.
BUILD_INPUTS = (
    DOCKERFILE,
    "pyproject.toml",
    "requirements-lock.txt",
    "cli",
    "gco",
    "dockerfiles/build_scratch_rootfs.py",
    "dockerfiles/runtime_smoke.py",
)
_SKIPPED_PARTS = frozenset({"__pycache__", ".mypy_cache", ".pytest_cache", ".ruff_cache"})

#: Host variables passed through to the container when set (bare ``-e NAME``,
#: so the value never appears on a command line). The AWS ones keep the host's
#: credential and Region choices; the GCO ones its configuration overrides.
FORWARDED_ENV = (
    "AWS_PROFILE",
    "AWS_DEFAULT_PROFILE",
    "AWS_REGION",
    "AWS_DEFAULT_REGION",
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN",
    "AWS_CREDENTIAL_EXPIRATION",
    "AWS_ROLE_ARN",
    "AWS_ROLE_SESSION_NAME",
    "AWS_ENDPOINT_URL",
    "AWS_USE_FIPS_ENDPOINT",
    "AWS_USE_DUALSTACK_ENDPOINT",
    "AWS_RETRY_MODE",
    "AWS_MAX_ATTEMPTS",
    "AWS_EC2_METADATA_DISABLED",
    "GCO_DEFAULT_REGION",
    "GCO_PROJECT_NAME",
    "GCO_GLOBAL_REGION",
    "GCO_API_GATEWAY_REGION",
    "GCO_MONITORING_REGION",
    "GCO_USE_REGIONAL_API",
)


class ContainerError(RuntimeError):
    """A container runtime command failed; the message says which and why."""


@dataclass(frozen=True)
class DashboardState:
    """Where a started dashboard runs (persisted in the state file)."""

    mode: str
    port: int
    token: str | None
    project_dir: str
    started_at: str
    runtime: str | None = None
    container: str | None = None
    image: str | None = None
    #: The process serving a ``serve`` dashboard, so other gco commands on this
    #: machine can tell whether it still runs (see :func:`process_alive`).
    pid: int | None = None

    @property
    def url(self) -> str:
        return dashboard_url(self.port, self.token)


def image_ref(runtime: str) -> str:
    """The local image reference: Podman resolves bare names against registries."""
    ref = f"{NAME}:{IMAGE_TAG}"
    return f"localhost/{ref}" if runtime == "podman" else ref


def find_build_context(*candidates: Path) -> Path | None:
    """The first of ``candidates`` (or one of their parents) holding ``Dockerfile.dashboard``."""
    for candidate in candidates:
        for directory in (candidate, *candidate.parents):
            if (directory / DOCKERFILE).is_file() and (directory / "cli").is_dir():
                return directory
    return None


def default_build_contexts() -> tuple[Path, ...]:
    """Where to look for the checkout: the one this CLI runs from, then the working directory."""
    return (Path(__file__).resolve().parents[2], Path.cwd())


def _input_files(root: Path) -> Iterable[Path]:
    for name in BUILD_INPUTS:
        path = root / name
        if path.is_file():
            yield path
        elif path.is_dir():
            for child in sorted(path.rglob("*")):
                relative = child.relative_to(root)
                if (
                    child.is_file()
                    and not (_SKIPPED_PARTS & set(relative.parts))
                    and child.suffix not in {".pyc", ".pyo"}
                ):
                    yield child


def source_fingerprint(root: Path) -> str:
    """SHA-256 over every build input's path and bytes (the image's source label)."""
    digest = hashlib.sha256()
    for path in _input_files(root):
        digest.update(path.relative_to(root).as_posix().encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _run(
    argv: Sequence[str],
    *,
    env: Mapping[str, str] | None = None,
    timeout: float | None = 120,
) -> subprocess.CompletedProcess[str]:
    """Run a runtime command (argument list, never a shell) and capture its output.

    A runtime that hangs past ``timeout`` (``subprocess.run`` stops it) or
    cannot be executed at all raises :class:`ContainerError`, which every
    ``gco dashboard`` command reports, rather than ending in a traceback.
    """
    try:
        return subprocess.run(
            list(argv),
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
            env=dict(env) if env is not None else None,
        )
    except subprocess.TimeoutExpired as exc:
        raise ContainerError(
            f"'{' '.join(argv[:3])}' did not finish within {exc.timeout:g}s and was stopped; "
            f"check that {argv[0]} is responding"
        ) from exc
    except OSError as exc:
        raise ContainerError(f"'{argv[0]}' could not run: {exc.strerror or exc}") from exc


def _failure(argv: Sequence[str], result: subprocess.CompletedProcess[str]) -> ContainerError:
    detail = (result.stderr or result.stdout or "").strip().splitlines()
    return ContainerError(
        f"'{' '.join(argv[:3])}' failed (exit {result.returncode})"
        + (f": {detail[-1]}" if detail else "")
    )


def image_fingerprint(runtime: str) -> str | None:
    """The source fingerprint the local image was built from, or None without one."""
    result = _run(
        [
            runtime,
            "image",
            "inspect",
            "--format",
            f'{{{{ index .Config.Labels "{SOURCE_LABEL}" }}}}',
            image_ref(runtime),
        ]
    )
    if result.returncode != 0:
        return None
    value = result.stdout.strip()
    return value if value and value != "<no value>" else None


def build_image(runtime: str, context: Path, fingerprint: str) -> None:
    """Build the dashboard image, streaming the runtime's progress to the terminal."""
    argv = [
        runtime,
        "build",
        "-f",
        str(context / DOCKERFILE),
        "-t",
        image_ref(runtime),
        "--label",
        f"{SOURCE_LABEL}={fingerprint}",
        str(context),
    ]
    result = subprocess.run(argv, check=False)
    if result.returncode != 0:
        raise ContainerError(f"'{runtime} build' failed (exit {result.returncode}); see above")


def container_status(runtime: str) -> str | None:
    """The dashboard container's state (``running``, ``exited``...), or None when absent."""
    result = _run([runtime, "inspect", "--format", "{{.State.Status}}", NAME])
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def remove_container(runtime: str) -> bool:
    """Stop and remove the dashboard container; False when there was none."""
    if container_status(runtime) is None:
        return False
    argv = [runtime, "rm", "-f", NAME]
    result = _run(argv)
    if result.returncode != 0:
        raise _failure(argv, result)
    return True


def _user_args(runtime: str) -> list[str]:
    """Run as the caller's uid so files private to them (``~/.aws``) are readable."""
    if runtime == "podman":
        return ["--userns", "keep-id"]
    getuid = getattr(os, "getuid", None)
    getgid = getattr(os, "getgid", None)
    if getuid is None or getgid is None:  # Windows: the runtime maps users itself
        return []
    return ["--user", f"{getuid()}:{getgid()}"]


def run_args(
    runtime: str,
    *,
    port: int,
    project_dir: Path,
    aws_dir: Path | None,
    environ: Mapping[str, str],
) -> list[str]:
    """The ``<runtime> run`` command line that starts the dashboard container.

    ``port`` is the host port the container's port is published on. The
    server inside learns it from ``GCO_DASHBOARD_BROWSER_PORT`` (a port
    number is no secret, so its value is on the command line) and names it
    in the address it prints.
    """
    argv = [
        runtime,
        "run",
        "--detach",
        "--name",
        NAME,
        "--label",
        f"{CONTAINER_LABEL}=1",
        "--publish",
        f"127.0.0.1:{port}:{CONTAINER_PORT}",
        "--read-only",
        "--tmpfs",
        CONTAINER_TMP,
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        *_user_args(runtime),
        "--env",
        f"HOME={CONTAINER_HOME}",
        "--env",
        "USER=gco",
        "--env",
        f"{BROWSER_PORT_ENV}={port}",
        "--env",
        TOKEN_ENV,
    ]
    for name in FORWARDED_ENV:
        if environ.get(name):
            argv += ["--env", name]
    if aws_dir is not None and aws_dir.is_dir():
        argv += ["--volume", f"{aws_dir}:{CONTAINER_HOME}/.aws:ro"]
    argv += [
        "--volume",
        f"{project_dir}:{CONTAINER_WORKSPACE}:ro",
        "--workdir",
        CONTAINER_WORKSPACE,
        image_ref(runtime),
    ]
    return argv


def start_container(
    runtime: str,
    *,
    port: int,
    token: str | None,
    project_dir: Path,
    aws_dir: Path | None,
) -> None:
    """Start the dashboard container (replacing a stopped one of the same name)."""
    remove_container(runtime)
    environ = dict(os.environ)
    if token:
        environ[TOKEN_ENV] = token
    else:
        environ.pop(TOKEN_ENV, None)
    argv = run_args(runtime, port=port, project_dir=project_dir, aws_dir=aws_dir, environ=environ)
    result = _run(argv, env=environ)
    if result.returncode != 0:
        raise _failure(argv, result)


def wait_until_healthy(port: int, *, timeout: float = 90.0, interval: float = 1.0) -> bool:
    """Poll the dashboard's health endpoint until it answers or ``timeout`` passes."""
    deadline = time.monotonic() + timeout
    while True:
        if healthy(port):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(interval)


def stream_logs(runtime: str, *, follow: bool, tail: int | None) -> int:
    """Print the container's logs (and keep printing with ``follow``); returns the exit code."""
    argv = [runtime, "logs"]
    if follow:
        argv.append("--follow")
    if tail is not None:
        argv += ["--tail", str(tail)]
    argv.append(NAME)
    with contextlib.suppress(KeyboardInterrupt):
        return subprocess.run(argv, check=False).returncode
    return 0


# ---------------------------------------------------------------------------
# State file
# ---------------------------------------------------------------------------


def state_path() -> Path:
    override = os.environ.get(STATE_ENV)
    return Path(override) if override else Path.home() / ".gco" / "dashboard.json"


def save_state(state: DashboardState) -> None:
    """Write the state file readable by its owner only (it holds the token)."""
    path = state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        json.dump(dataclasses.asdict(state), handle, indent=2)
    with contextlib.suppress(OSError):
        path.chmod(0o600)


def load_state() -> DashboardState | None:
    """The last saved state, or None when there is none or it cannot be read."""
    try:
        raw: Any = json.loads(state_path().read_text(encoding="utf-8"))
        fields = {field.name for field in dataclasses.fields(DashboardState)}
        return DashboardState(**{key: value for key, value in raw.items() if key in fields})
    except OSError, ValueError, TypeError, AttributeError:
        return None


def clear_state() -> None:
    with contextlib.suppress(FileNotFoundError):
        state_path().unlink()


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def process_alive(pid: int | None) -> bool | None:
    """Whether process ``pid`` runs on this machine, or None when that cannot be told.

    Signal 0 asks the kernel about a process without signalling it. Windows
    has no such check (there signal 0 is a Ctrl-C event, which would stop the
    dashboard), and a missing or malformed PID (the state file is read back
    as JSON) names no process.
    """
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0 or sys.platform == "win32":
        return None
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:  # it runs, as another user
        return True
    except OverflowError:  # no process can have that PID
        return False
    return True


def in_dev_container() -> bool:
    """Whether this process runs in the ``gco-dev`` image (``Dockerfile.dev`` sets the marker)."""
    return os.environ.get("GCO_DEV_CONTAINER") == "1"
