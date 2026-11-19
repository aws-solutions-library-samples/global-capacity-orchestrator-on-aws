#!/usr/bin/env python3
"""Import every deployed Python Lambda handler in the environment it deploys with.

The project's environment holds every package the CLI and the services use, so
a handler that imports a package its deployment does not ship would import
there and fail only in AWS. Each handler is imported instead by a fresh,
isolated interpreter (``python -I``) whose only third-party packages are what
that Lambda has in AWS:

* the packages the Lambda Python runtime provides, pinned at the runtime's
  versions in ``.github/config/lambda-python-runtime.txt`` (one virtualenv,
  built with uv and shared, since the runtime is the same for every Lambda);
* the files its deployment packages, first on ``sys.path`` as ``/var/task`` is:
  the source directory for a Lambda zipped as committed
  (``lambda_source_code``), the directory the build action stages for
  ``kubectl-applier-simple`` (its requirements.txt already installed beside
  ``handler.py``), or, for a container image, the files its Dockerfile copies
  into the task root plus its requirements.txt installed there, as the image
  build does.

A Lambda zipped from its source directory deploys no requirements.txt, so its
handler may import only the standard library and the runtime's packages.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from contextlib import AbstractContextManager
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

#: Handler entrypoints other than ``lambda_handler``, per source directory. The
#: stacks name them in each function's ``handler=`` (tests/test_ci_runtime_verifiers.py
#: holds this table to gco/stacks).
ENTRYPOINT_OVERRIDES: dict[str, tuple[str, ...]] = {
    "analytics-cleanup": ("handler",),
    "ga-registration": ("lambda_handler", "on_delete_event"),
    "helm-orchestrator": ("on_event",),
}
DEFAULT_ENTRYPOINTS = ("lambda_handler",)

#: Modules other than ``handler.py`` that a stack deploys from a source
#: directory zipped as committed, with their entrypoints.
EXTRA_SOURCE_MODULES: dict[str, dict[str, tuple[str, ...]]] = {
    "helm-installer": {
        "teardown_provider": ("drain_install_executions", "is_complete", "on_event"),
    },
}

#: Lambdas whose zip is the directory the build action stages
#: (``cli.stacks.prepare_cdk_assets``) rather than the source directory.
BUILD_DIRECTORIES = {"kubectl-applier-simple": "kubectl-applier-simple-build"}

#: Lambdas deployed as a container image, and the Docker context the stack
#: builds it from (under ``lambda/``). The handler module comes from the
#: Dockerfile's ``CMD``.
IMAGE_CONTEXTS = {
    "helm-installer": "helm-installer-build",
    "tls-certificate-manager": "tls-certificate-manager",
}

#: The runtime package pins, relative to the repository root.
RUNTIME_PACKAGES = Path(".github/config/lambda-python-runtime.txt")

#: The checkout this script belongs to, whose ``cli`` package it imports.
REPO_ROOT = Path(__file__).resolve().parents[2]

IMPORT_TIMEOUT_SECONDS = 30
UV_TIMEOUT_SECONDS = 300

#: Run by the runtime interpreter: put the task root(s) first on ``sys.path``,
#: import the module and require every entrypoint to be callable.
IMPORT_CHECK = (
    "import importlib,os,sys;"
    "sys.path[:0]=sys.argv[1].split(os.pathsep);"
    "module=importlib.import_module(sys.argv[2]);"
    "missing=[name for name in sys.argv[3:] if not callable(getattr(module,name,None))];"
    "raise SystemExit('not callable: '+', '.join(missing) if missing else 0)"
)

Packaging = Literal["source", "build", "image"]

_RUNTIME_LINE = re.compile(r"^#\s*lambda-runtime:\s*(?P<runtime>python\d+\.\d+)\s*$", re.MULTILINE)
_PIN = re.compile(r"(?P<name>[A-Za-z0-9][A-Za-z0-9._-]*)==(?P<version>[A-Za-z0-9.+!_-]+)")
_TASK_ROOT = "${LAMBDA_TASK_ROOT}"


@dataclass(frozen=True)
class RuntimePackages:
    """The runtime's package pins and the runtime they were read from."""

    path: Path
    runtime: str
    pins: dict[str, str]


@dataclass(frozen=True)
class HandlerTarget:
    """One deployed handler module and how its code reaches Lambda.

    Attributes:
        name: The source directory under ``lambda/``.
        module: The module the handler string names (``handler`` in
            ``handler.lambda_handler``).
        entrypoints: The callables the stacks (or the image ``CMD``) name.
        packaging: ``source`` (zipped as committed), ``build`` (the staged
            build directory is zipped) or ``image`` (container image).
        code_directory: What the stack packages: the source directory, the
            staged build directory, or the image's Docker context.
        task_files: For an image, the files its Dockerfile copies into the
            task root.
    """

    name: str
    module: str
    entrypoints: tuple[str, ...]
    packaging: Packaging
    code_directory: Path
    task_files: tuple[str, ...] = ()

    @property
    def label(self) -> str:
        """``lambda/<name> <module>.<entrypoints> (<packaging>)`` for logs."""
        return f"lambda/{self.name} {self.module}.{'/'.join(self.entrypoints)} ({self.packaging})"


def load_runtime_packages(path: Path) -> RuntimePackages:
    """Read the runtime pin file: exact ``name==version`` lines and its runtime line."""
    text = path.read_text(encoding="utf-8")
    runtime = _RUNTIME_LINE.search(text)
    if runtime is None:
        raise RuntimeError(f"{path}: no '# lambda-runtime: pythonX.Y' line")
    pins: dict[str, str] = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        pin = _PIN.fullmatch(stripped)
        if pin is None:
            raise RuntimeError(f"{path}: {stripped!r} is not an exact name==version pin")
        pins[pin["name"].lower()] = pin["version"]
    missing = sorted({"boto3", "botocore"} - set(pins))
    if missing:
        raise RuntimeError(f"{path}: no pin for {', '.join(missing)}")
    return RuntimePackages(path=path, runtime=runtime["runtime"], pins=pins)


def _dockerfile_instructions(text: str) -> list[tuple[str, str]]:
    """``(INSTRUCTION, arguments)`` per logical Dockerfile line (continuations joined)."""
    instructions: list[tuple[str, str]] = []
    pending = ""
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.endswith("\\"):
            pending += line[:-1] + " "
            continue
        keyword, _, arguments = (pending + line).partition(" ")
        instructions.append((keyword.upper(), arguments.strip()))
        pending = ""
    if pending:
        raise RuntimeError("the last instruction ends with a line continuation")
    return instructions


def read_image_plan(dockerfile: Path) -> tuple[tuple[str, ...], str, str]:
    """``(task-root files, module, entrypoint)`` of a Lambda image's Dockerfile.

    Only the forms GCO's Lambda Dockerfiles use are accepted: one ``FROM``,
    ``WORKDIR ${LAMBDA_TASK_ROOT}``, ``COPY <file>... .`` into the task root,
    and an exec-form ``CMD ["module.function"]``. A Dockerfile that starts
    placing code another way fails here, instead of being modelled wrongly.
    """
    try:
        instructions = _dockerfile_instructions(dockerfile.read_text(encoding="utf-8"))
    except RuntimeError as exc:
        raise RuntimeError(f"{dockerfile}: {exc}") from exc
    if [keyword for keyword, _ in instructions].count("FROM") != 1:
        raise RuntimeError(f"{dockerfile}: expected exactly one FROM (no multi-stage build)")
    workdir = ""
    files: list[str] = []
    command: tuple[str, str] | None = None
    for keyword, arguments in instructions:
        if keyword == "WORKDIR":
            workdir = arguments
        elif keyword in {"COPY", "ADD"}:
            tokens = shlex.split(arguments)
            destination_is_task_root = len(tokens) >= 2 and (
                tokens[-1].rstrip("/") == _TASK_ROOT
                or (tokens[-1] in {".", "./"} and workdir == _TASK_ROOT)
            )
            if (
                keyword == "ADD"
                or not destination_is_task_root
                or any(token.startswith("-") or any(c in token for c in "*?[") for token in tokens)
            ):
                raise RuntimeError(
                    f"{dockerfile}: unsupported {keyword} {arguments!r}; teach "
                    ".github/scripts/verify_lambda_imports.py this form"
                )
            files.extend(tokens[:-1])
        elif keyword == "CMD":
            try:
                parsed = json.loads(arguments)
            except json.JSONDecodeError:
                parsed = None
            handler = parsed[0] if isinstance(parsed, list) and len(parsed) == 1 else None
            module, _, entrypoint = (
                handler.rpartition(".") if isinstance(handler, str) else ("", "", "")
            )
            if not module or not entrypoint:
                raise RuntimeError(
                    f'{dockerfile}: CMD must be ["module.function"], not {arguments}'
                )
            command = (module, entrypoint)
    if command is None:
        raise RuntimeError(f"{dockerfile}: no CMD names the handler")
    return tuple(files), command[0], command[1]


def discover_handlers(root: Path) -> list[HandlerTarget]:
    """Every deployed handler under ``lambda/``, with how its code is packaged.

    Discovery starts from each non-generated ``lambda/*/handler.py``; the
    tables above add the entrypoints, the extra modules, and the build and
    image packaging. A table entry naming no handler directory is stale and
    fails, so a renamed Lambda cannot silently lose its settings.
    """
    lambda_root = root.resolve() / "lambda"
    names = sorted(
        path.parent.name
        for path in lambda_root.glob("*/handler.py")
        if not path.parent.name.endswith("-build")
    )
    if not names:
        raise RuntimeError(f"no Python Lambda handlers found under {lambda_root}")
    configured = {
        *ENTRYPOINT_OVERRIDES,
        *EXTRA_SOURCE_MODULES,
        *BUILD_DIRECTORIES,
        *IMAGE_CONTEXTS,
    }
    stale = sorted(configured - set(names))
    if stale:
        raise RuntimeError(f"verifier settings reference missing handlers: {', '.join(stale)}")

    targets: list[HandlerTarget] = []
    for name in names:
        entrypoints = ENTRYPOINT_OVERRIDES.get(name, DEFAULT_ENTRYPOINTS)
        if name in IMAGE_CONTEXTS:
            files, module, entrypoint = read_image_plan(lambda_root / name / "Dockerfile")
            targets.append(
                HandlerTarget(
                    name=name,
                    module=module,
                    entrypoints=(entrypoint,),
                    packaging="image",
                    code_directory=lambda_root / IMAGE_CONTEXTS[name],
                    task_files=files,
                )
            )
        elif name in BUILD_DIRECTORIES:
            targets.append(
                HandlerTarget(
                    name=name,
                    module="handler",
                    entrypoints=entrypoints,
                    packaging="build",
                    code_directory=lambda_root / BUILD_DIRECTORIES[name],
                )
            )
        else:
            targets.append(
                HandlerTarget(
                    name=name,
                    module="handler",
                    entrypoints=entrypoints,
                    packaging="source",
                    code_directory=lambda_root / name,
                )
            )
        for module, module_entrypoints in EXTRA_SOURCE_MODULES.get(name, {}).items():
            targets.append(
                HandlerTarget(
                    name=name,
                    module=module,
                    entrypoints=module_entrypoints,
                    packaging="source",
                    code_directory=lambda_root / name,
                )
            )
    return targets


def _run_uv(uv: str, *arguments: str) -> None:
    subprocess.run(  # nosemgrep: dangerous-subprocess-use-audit - fixed uv arguments and script-owned paths
        [uv, *arguments],
        check=True,
        timeout=UV_TIMEOUT_SECONDS,
    )


def build_runtime_environment(uv: str, directory: Path, packages: RuntimePackages) -> Path:
    """Create the runtime virtualenv (this interpreter, the pinned packages only).

    Returns its interpreter.
    """
    _run_uv(uv, "venv", "--no-config", "--quiet", "--python", sys.executable, str(directory))
    python = directory / "bin" / "python"
    _run_uv(
        uv,
        "pip",
        "install",
        "--no-config",
        "--python",
        str(python),
        "--no-deps",
        "--requirement",
        str(packages.path),
    )
    return python


def stage_target(target: HandlerTarget, *, uv: str, python: Path, scratch: Path) -> list[Path]:
    """The directories that stand for ``/var/task``, staged for *target*.

    A zipped Lambda's directory is used where it is. An image's task root is
    assembled in *scratch* from the files its Dockerfile copies, with its
    requirements.txt installed into it, as ``pip install --target`` does.
    """
    if not target.code_directory.is_dir():
        raise RuntimeError(
            f"{target.code_directory} is missing; stage it first with the build action "
            "(python -c 'from cli.stacks import prepare_cdk_assets; prepare_cdk_assets(\".\")')"
        )
    if target.packaging != "image":
        return [target.code_directory]
    task_root = scratch / f"task-{target.name}"
    task_root.mkdir()
    for name in target.task_files:
        source = target.code_directory / name
        if source.is_dir():
            shutil.copytree(source, task_root / name)
        else:
            shutil.copy2(source, task_root / name)
    if "requirements.txt" in target.task_files:
        _run_uv(
            uv,
            "pip",
            "install",
            "--no-config",
            "--python",
            str(python),
            "--target",
            str(task_root),
            "--requirement",
            str(task_root / "requirements.txt"),
        )
    return [task_root]


def _child_environment() -> dict[str, str]:
    environment = os.environ.copy()
    # Several handlers create boto3 clients at import time, which needs a
    # Region even though no AWS call is made.
    environment.setdefault("AWS_DEFAULT_REGION", "us-east-1")
    environment.setdefault("AWS_REGION", "us-east-1")
    environment.setdefault("AWS_EC2_METADATA_DISABLED", "true")
    return environment


def import_target(
    target: HandlerTarget,
    *,
    python: Path,
    paths: list[Path],
    timeout: int = IMPORT_TIMEOUT_SECONDS,
) -> str | None:
    """Import *target* with the runtime interpreter; ``None`` or the failure text.

    ``-I`` keeps the current directory, ``PYTHONPATH`` and the user
    site-packages out of ``sys.path``; ``-B`` keeps bytecode out of the
    checkout and the staged build directories.
    """
    result = subprocess.run(  # nosemgrep: dangerous-subprocess-use-audit - fixed interpreter flags and script-owned paths
        [
            str(python),
            "-I",
            "-B",
            "-c",
            IMPORT_CHECK,
            os.pathsep.join(str(path) for path in paths),
            target.module,
            *target.entrypoints,
        ],
        capture_output=True,
        text=True,
        env=_child_environment(),
        timeout=timeout,
        check=False,
    )
    if result.returncode == 0:
        return None
    detail = "\n".join(result.stderr.strip().splitlines()[-15:]) or f"exit {result.returncode}"
    return f"{target.label} failed:\n{detail}\n  {_packaging_hint(target)}"


def _packaging_hint(target: HandlerTarget) -> str:
    """What the failed import could see, in the words of its packaging."""
    runtime = f"the standard library and the Lambda runtime's packages ({RUNTIME_PACKAGES})"
    if target.packaging == "source":
        return (
            f"lambda/{target.name} deploys as a zip of its source directory, so only its own "
            f"files, {runtime} are importable; its requirements.txt is not installed."
        )
    if target.packaging == "build":
        return (
            f"lambda/{target.name} deploys lambda/{target.code_directory.name}, which the build "
            f"action stages (cli/stacks.py); only what it stages and {runtime} are importable."
        )
    return (
        f"the lambda/{target.name} image's task root holds only {', '.join(target.task_files)} "
        f"and what its requirements.txt installs, on top of {runtime}."
    )


def host_loads_staged_wheels() -> bool:
    """Whether this host can load the wheels the build action stages.

    ``cli/stacks.py`` installs the kubectl-applier-simple requirements for the
    Lambda platform (``--platform manylinux2014_x86_64``), whatever the host,
    so their compiled extensions load only on Linux x86_64, as CI runs.
    """
    return sys.platform == "linux" and platform.machine().lower() in {"x86_64", "amd64"}


@dataclass(frozen=True)
class Outcome:
    """What :func:`verify_handlers` found."""

    failures: list[str]
    skipped: list[str]


def verify_handlers(targets: list[HandlerTarget], *, uv: str, packages: RuntimePackages) -> Outcome:
    """Import every target in its own environment.

    A ``build`` target is skipped, and reported, on a host that cannot load
    the Lambda platform's wheels (a developer's macOS machine); CI runs on
    Linux x86_64 and imports every target.
    """
    outcome = Outcome(failures=[], skipped=[])
    with tempfile.TemporaryDirectory(prefix="gco-lambda-imports-") as scratch_name:
        scratch = Path(scratch_name)
        python = build_runtime_environment(uv, scratch / "runtime", packages)
        for target in targets:
            print(f"=== {target.label} ===", flush=True)
            if target.packaging == "build" and not host_loads_staged_wheels():
                note = (
                    f"{target.label} skipped: lambda/{target.code_directory.name} holds wheels "
                    f"for Linux x86_64, which this {sys.platform} {platform.machine()} host "
                    "cannot load"
                )
                print(note, flush=True)
                outcome.skipped.append(note)
                continue
            paths = stage_target(target, uv=uv, python=python, scratch=scratch)
            failure = import_target(target, python=python, paths=paths)
            if failure is not None:
                print(failure, flush=True)
                outcome.failures.append(failure)
    return outcome


def _asset_consumer(root: Path) -> AbstractContextManager[None]:
    """Hold the build action's directories source-current while they are imported.

    ``cdk_asset_consumer`` is the CLI's entry point for anything that reads
    the generated ``*-build`` directories: it rebuilds a stale one first and
    keeps it unchanged until the block exits. It comes from this script's own
    checkout, whatever *root* is.
    """
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))
    from cli.stacks import cdk_asset_consumer

    return cdk_asset_consumer(root)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root",
        type=Path,
        default=REPO_ROOT,
        help="Repository root (defaults to the checkout containing this script)",
    )
    parser.add_argument("--uv", help="uv executable (defaults to uv on PATH)")
    parser.add_argument(
        "--require-all",
        action="store_true",
        help="fail instead of skipping a target this host cannot import (CI sets this)",
    )
    args = parser.parse_args(argv)
    root = args.root.resolve()
    uv = args.uv or shutil.which("uv")
    if uv is None:
        parser.error(
            "uv is required; install the pinned one: bash .github/scripts/use-pinned-uv.sh"
        )
    packages = load_runtime_packages(root / RUNTIME_PACKAGES)
    interpreter = f"python{sys.version_info.major}.{sys.version_info.minor}"
    if interpreter != packages.runtime:
        parser.error(
            f"run this check under {packages.runtime}, the Lambda runtime "
            f"{RUNTIME_PACKAGES} describes (this is {interpreter})"
        )
    targets = discover_handlers(root)
    with _asset_consumer(root):
        outcome = verify_handlers(targets, uv=uv, packages=packages)
    if outcome.failures:
        print(f"{len(outcome.failures)} of {len(targets)} Python Lambda handlers failed to import.")
        return 1
    if outcome.skipped and args.require_all:
        print(f"--require-all: {len(outcome.skipped)} handlers were skipped on this host.")
        return 1
    imported = len(targets) - len(outcome.skipped)
    skipped = f"; skipped {len(outcome.skipped)} (see above)" if outcome.skipped else ""
    print(f"Imported {imported} Python Lambda handlers{skipped}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
