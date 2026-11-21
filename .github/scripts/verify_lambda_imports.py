#!/usr/bin/env python3
"""Import every zipped Python Lambda handler inside the Lambda Python base image.

The project's environment holds every package the CLI and the services use, so
a handler that imports a package its deployment does not ship would import
there and fail only in AWS. Each handler is imported instead by the Lambda
runtime's own interpreter, in a fresh container of the Lambda Python base image
with no network. The image reference is read from the FROM lines of the image
Lambdas' Dockerfiles (``lambda/*/Dockerfile``), so this check runs on the image
those Lambdas build on, including any digest pinned there. The handler's
deployed code is mounted read-only at ``/var/task`` and goes first on
``sys.path``, as the Lambda bootstrap puts it:

* the source directory for a Lambda zipped as committed
  (``lambda_source_code``);
* the directory the build action stages for ``kubectl-applier-simple`` (its
  requirements.txt already installed beside ``handler.py``).

From the image's own site-packages a handler may rely only on the SDK the
runtime provides: boto3 and the closure of its install requirements, without
extras, computed in the container from the image's package metadata. The
image's other modules (pip, and the runtime interface client with its
dependencies) cannot be imported unless the deployed code ships its own copy.
A Lambda zipped from its source directory deploys no requirements.txt, so its
handler may import only its own files, the standard library and that SDK.

The container-image Lambdas are not imported here: their ``integration:docker:*``
jobs import every entrypoint inside the image they build
(``tests/test_ci_runtime_verifiers.py`` holds those steps to ``gco/stacks``).
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
import time
from collections.abc import Callable, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol

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

#: The Lambda Python base image: a tag is required, a digest optional.
BASE_IMAGE = re.compile(
    r"public\.ecr\.aws/lambda/python:[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}(?:@sha256:[0-9a-f]{64})?"
)
_FROM = re.compile(r"^[ \t]*FROM[ \t]+(?P<reference>[^\n]*?)[ \t]*$", re.IGNORECASE | re.MULTILINE)

#: Every GCO Python Lambda runs on x86_64.
PLATFORM = "linux/amd64"
#: The runtime's interpreter in the base image (``/var/runtime/bootstrap`` runs it).
IMAGE_PYTHON = "/var/lang/bin/python3"
TASK_ROOT = "/var/task"
#: Several handlers create boto3 clients at import time, which needs a Region
#: even though no AWS call is made.
REGION = "us-east-1"

#: The check program's own limit on one import (a faulthandler watchdog in the
#: container), and the host's limit on the container around it.
IMPORT_TIMEOUT_SECONDS = 30
CONTAINER_TIMEOUT_SECONDS = IMPORT_TIMEOUT_SECONDS + 60
#: ECR Public throttles anonymous pulls (one request per second, and a data
#: limit per address that GitHub's shared runner addresses reach), answering
#: 429; see .github/scripts/preload_kind_images.py. A refused pull is retried.
PULL_TIMEOUT_SECONDS = 300
PULL_ATTEMPTS = 5
PULL_DELAY_SECONDS = 15.0

#: The checkout this script belongs to, whose ``cli`` package it imports.
REPO_ROOT = Path(__file__).resolve().parents[2]

#: Run in the container as ``python3 -I -B -c CHECK_PROGRAM <timeout> <task root>
#: <module> <entrypoint>...``. Before the task root goes on ``sys.path`` it
#: computes, from the image's package metadata, the runtime's SDK (boto3 and the
#: requirements it installs with no extras, markers evaluated for this
#: interpreter) and every other top-level module the image's site-packages
#: provides. Those others are refused by a ``sys.meta_path`` finder unless the
#: task root itself holds a module or package of that name. Then it puts the
#: task root first on ``sys.path`` and processes its ``.pth`` files, as
#: ``/var/runtime/bootstrap.py`` does, imports the module and requires every
#: entrypoint to be callable. On success it prints the SDK and the refused
#: modules. A marker or requirement it cannot evaluate is an error, not a guess.
CHECK_PROGRAM = r"""
import ast
import faulthandler
import importlib
import importlib.machinery
import importlib.metadata
import inspect
import operator
import os
import platform
import re
import site
import sys

timeout, task_root, module_name, *entrypoints = sys.argv[1:]
faulthandler.dump_traceback_later(int(timeout), exit=True)
if not os.listdir(task_root):
    raise SystemExit(
        f"{task_root} is empty: the code directory was not mounted (a VM-backed container "
        "runtime mounts only the host paths it shares with its VM)"
    )


def canonical(name):
    return re.sub(r"[-_.]+", "-", name).lower()


implementation = sys.implementation.version
MARKERS = {
    "extra": "",
    "implementation_name": sys.implementation.name,
    "implementation_version": f"{implementation.major}.{implementation.minor}."
    f"{implementation.micro}"
    + ("" if implementation.releaselevel == "final"
       else implementation.releaselevel[0] + str(implementation.serial)),
    "os_name": os.name,
    "platform_machine": platform.machine(),
    "platform_python_implementation": platform.python_implementation(),
    "platform_release": platform.release(),
    "platform_system": platform.system(),
    "platform_version": platform.version(),
    "python_full_version": platform.python_version(),
    "python_version": f"{sys.version_info.major}.{sys.version_info.minor}",
    "sys_platform": sys.platform,
}
COMPARE = {ast.Eq: operator.eq, ast.NotEq: operator.ne, ast.Lt: operator.lt,
           ast.LtE: operator.le, ast.Gt: operator.gt, ast.GtE: operator.ge}


def unsupported(marker):
    return ValueError(f"cannot evaluate the environment marker {marker!r}")


def operand(node, marker):
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Name) and node.id in MARKERS:
        return MARKERS[node.id]
    raise unsupported(marker)


def release(value):
    return tuple(map(int, value.split("."))) if re.fullmatch(r"\d+(?:\.\d+)*", value) else None


def holds(node, marker):
    if isinstance(node, ast.BoolOp):
        results = [holds(value, marker) for value in node.values]
        return all(results) if isinstance(node.op, ast.And) else any(results)
    if not isinstance(node, ast.Compare) or len(node.ops) != 1:
        raise unsupported(marker)
    operation = type(node.ops[0])
    left, right = operand(node.left, marker), operand(node.comparators[0], marker)
    if operation in (ast.In, ast.NotIn):
        return (left in right) == (operation is ast.In)
    if operation not in COMPARE:
        raise unsupported(marker)
    versions = release(left), release(right)
    if None not in versions:
        width = max(map(len, versions))
        left, right = (version + (0,) * (width - len(version)) for version in versions)
    elif operation not in (ast.Eq, ast.NotEq):
        raise unsupported(marker)
    return COMPARE[operation](left, right)


REQUIREMENT = re.compile(r"\s*([A-Za-z0-9][A-Za-z0-9._-]*)[^;]*(?:;(.*))?", re.DOTALL)


def requirements(distribution):
    for text in distribution.requires or ():
        match = REQUIREMENT.fullmatch(text)
        if match is None:
            raise ValueError(f"cannot parse the requirement {text!r} of {distribution.name}")
        marker = (match[2] or "").strip()
        if marker:
            try:
                tree = ast.parse(marker, mode="eval").body
            except SyntaxError:
                raise unsupported(marker) from None
            if not holds(tree, marker):
                continue
        yield canonical(match[1])


sdk, pending = set(), ["boto3"]
while pending:
    name = canonical(pending.pop())
    if name not in sdk:
        sdk.add(name)
        pending.extend(requirements(importlib.metadata.distribution(name)))
owners = importlib.metadata.packages_distributions()
allowed = {module for module, names in owners.items() if any(canonical(n) in sdk for n in names)}
provided = set(owners)
for directory in site.getsitepackages():
    if os.path.isdir(directory):
        for entry in os.listdir(directory):
            path = os.path.join(directory, entry)
            provided.add(entry if os.path.isdir(path) else inspect.getmodulename(path) or "")
blocked = {
    name for name in provided - allowed
    if name.isidentifier() and name != "__pycache__" and name not in sys.stdlib_module_names
}


class RuntimeOnly:
    @staticmethod
    def find_spec(name, path=None, target=None):
        if name in blocked:
            shipped = importlib.machinery.PathFinder.find_spec(name, [task_root])
            if shipped is None or shipped.loader is None:
                raise ModuleNotFoundError(
                    f"No module named {name!r}: the Lambda runtime's site-packages has it, "
                    f"but handlers get only the runtime's SDK ({' '.join(sorted(sdk))}); "
                    "deploy it with the handler",
                    name=name,
                )
        return None


for name in [name for name in sys.modules if name.partition(".")[0] in blocked]:
    del sys.modules[name]
sys.meta_path.insert(0, RuntimeOnly)
sys.path.insert(0, task_root)
site.addsitedir(task_root)
module = importlib.import_module(module_name)
missing = [name for name in entrypoints if not callable(getattr(module, name, None))]
if missing:
    raise SystemExit("not callable: " + ", ".join(missing))
print(f"Runtime SDK: {' '.join(sorted(sdk))}. Refused unless the deployment ships them: "
      f"{' '.join(sorted(blocked)) or '(none)'}.")
"""

Packaging = Literal["source", "build"]


@dataclass(frozen=True)
class HandlerTarget:
    """One deployed handler module and how its code reaches Lambda.

    Attributes:
        name: The source directory under ``lambda/``.
        module: The module the handler string names (``handler`` in
            ``handler.lambda_handler``).
        entrypoints: The callables the stacks name.
        packaging: ``source`` (zipped as committed) or ``build`` (the staged
            build directory is zipped).
        code_directory: What the stack zips: the source directory or the
            staged build directory.
    """

    name: str
    module: str
    entrypoints: tuple[str, ...]
    packaging: Packaging
    code_directory: Path

    @property
    def label(self) -> str:
        """``lambda/<name> <module>.<entrypoints> (<packaging>)`` for logs."""
        return f"lambda/{self.name} {self.module}.{'/'.join(self.entrypoints)} ({self.packaging})"


class Runner(Protocol):
    """Runs a command, capturing its text output or letting it stream."""

    def __call__(
        self, command: Sequence[str], *, timeout: float, capture: bool
    ) -> subprocess.CompletedProcess[str]:
        """Run *command*; ``subprocess.TimeoutExpired`` once *timeout* seconds pass."""


def run_command(
    command: Sequence[str], *, timeout: float, capture: bool
) -> subprocess.CompletedProcess[str]:
    """Run *command*; ``subprocess.TimeoutExpired`` once *timeout* seconds pass."""
    return subprocess.run(  # nosemgrep: dangerous-subprocess-use-audit - fixed container CLI arguments and script-owned paths
        list(command),
        capture_output=capture,
        text=True,
        timeout=timeout,
        check=False,
    )


def discover_handlers(root: Path) -> list[HandlerTarget]:
    """Every zipped handler under ``lambda/``, with how its code is packaged.

    Discovery starts from each non-generated ``lambda/*/handler.py``; a
    directory with a ``Dockerfile`` is a container image, whose handler its
    ``integration:docker:*`` job imports, and only its ``EXTRA_SOURCE_MODULES``
    are zipped. The tables above add the entrypoints, the extra modules and the
    build packaging. A table entry naming no handler directory is stale and
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
    configured = {*ENTRYPOINT_OVERRIDES, *EXTRA_SOURCE_MODULES, *BUILD_DIRECTORIES}
    stale = sorted(configured - set(names))
    if stale:
        raise RuntimeError(f"verifier settings reference missing handlers: {', '.join(stale)}")

    targets: list[HandlerTarget] = []
    for name in names:
        source = lambda_root / name
        if not (source / "Dockerfile").is_file():
            build = BUILD_DIRECTORIES.get(name)
            targets.append(
                HandlerTarget(
                    name=name,
                    module="handler",
                    entrypoints=ENTRYPOINT_OVERRIDES.get(name, DEFAULT_ENTRYPOINTS),
                    packaging="source" if build is None else "build",
                    code_directory=source if build is None else lambda_root / build,
                )
            )
        for module, module_entrypoints in EXTRA_SOURCE_MODULES.get(name, {}).items():
            targets.append(
                HandlerTarget(
                    name=name,
                    module=module,
                    entrypoints=module_entrypoints,
                    packaging="source",
                    code_directory=source,
                )
            )
    return targets


def image_dockerfiles(root: Path) -> list[Path]:
    """The image Lambdas' Dockerfiles: ``lambda/*/Dockerfile``, generated copies excluded."""
    return sorted(
        path
        for path in (root / "lambda").glob("*/Dockerfile")
        if not path.parent.name.endswith("-build")
    )


def read_base_image(root: Path) -> str:
    """The Lambda Python base image every image Lambda's Dockerfile builds FROM.

    Each FROM must name ``public.ecr.aws/lambda/python:<tag>``, optionally
    ``@sha256:<digest>``, and all of them the same reference: the handlers are
    checked on the image the image Lambdas ship on.
    """
    dockerfiles = image_dockerfiles(root)
    if not dockerfiles:
        raise RuntimeError(
            f"no lambda/*/Dockerfile under {root} to read the Lambda Python base image from"
        )
    users: dict[str, list[str]] = {}
    for dockerfile in dockerfiles:
        name = dockerfile.relative_to(root).as_posix()
        references = [found["reference"] for found in _FROM.finditer(dockerfile.read_text("utf-8"))]
        if not references:
            raise RuntimeError(f"{name} has no FROM line")
        for reference in references:
            if BASE_IMAGE.fullmatch(reference) is None:
                raise RuntimeError(
                    f"{name}: FROM {reference} is not the Lambda Python base image "
                    "(public.ecr.aws/lambda/python:<tag>, optionally @sha256:<digest>)"
                )
            users.setdefault(reference, []).append(name)
    if len(users) > 1:
        detail = "; ".join(
            f"{reference} in {', '.join(names)}" for reference, names in users.items()
        )
        raise RuntimeError(f"the Lambda image Dockerfiles disagree on the base image: {detail}")
    return next(iter(users))


def _use_this_checkout() -> None:
    """Import ``cli`` from this script's own checkout, whatever ``--root`` is."""
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))


def container_cli() -> str:
    """The container CLI, chosen as ``gco stacks deploy`` chooses CDK's.

    ``CDK_DOCKER`` when set; otherwise the first of docker, finch and podman
    that answers ``<cli> info`` (``cli._container_runtime``).
    """
    _use_this_checkout()
    from cli._container_runtime import container_runtime_error_message, detect_container_runtime

    runtime = detect_container_runtime()
    if runtime is None:
        raise RuntimeError(container_runtime_error_message(allow_cdk_docker=True))
    return runtime


def _excerpt(stderr: str) -> str:
    """Why an import failed: the end of its traceback, or, when the watchdog
    stopped it, the start of the stack dump, which holds the innermost frames."""
    lines = stderr.strip().splitlines()
    for index, line in enumerate(lines):
        if line.startswith("Timeout (") and line.endswith(")!"):
            return "\n".join(
                [
                    f"the import did not finish within {IMPORT_TIMEOUT_SECONDS} s; "
                    "the stack when it was stopped:",
                    *lines[index : index + 15],
                ]
            )
    return "\n".join(lines[-15:])


def pull_image(
    cli: str,
    image: str,
    *,
    runner: Runner = run_command,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    """Pull *image* for :data:`PLATFORM` once, before any import, retrying a refusal."""
    command = [cli, "pull", "--platform", PLATFORM, image]
    for attempt in range(1, PULL_ATTEMPTS + 1):
        print(f"$ {' '.join(command)}  (attempt {attempt}/{PULL_ATTEMPTS})", flush=True)
        try:
            result = runner(command, timeout=PULL_TIMEOUT_SECONDS, capture=False)
        except subprocess.TimeoutExpired:
            outcome = f"no result within {PULL_TIMEOUT_SECONDS} s"
        except OSError as exc:
            raise RuntimeError(f"cannot run the container CLI {cli!r}: {exc}") from exc
        else:
            if result.returncode == 0:
                return
            outcome = f"exit {result.returncode}"
        if attempt < PULL_ATTEMPTS:
            print(f"Pull failed ({outcome}); retrying in {PULL_DELAY_SECONDS:.0f} s.", flush=True)
            sleep(PULL_DELAY_SECONDS)
    raise RuntimeError(
        f"could not pull {image} ({PLATFORM}) with {cli}: the last of {PULL_ATTEMPTS} "
        f"attempts ended with {outcome} (its output is above)"
    )


def import_command(target: HandlerTarget, *, cli: str, image: str) -> list[str]:
    """The container run that imports *target*: read-only task root, no network."""
    return [
        cli,
        "run",
        "--rm",
        "--platform",
        PLATFORM,
        "--pull",
        "never",
        "--network",
        "none",
        "-v",
        f"{target.code_directory}:{TASK_ROOT}:ro",
        "-e",
        f"AWS_REGION={REGION}",
        "-e",
        f"AWS_DEFAULT_REGION={REGION}",
        "--entrypoint",
        IMAGE_PYTHON,
        image,
        "-I",
        "-B",
        "-c",
        CHECK_PROGRAM,
        str(IMPORT_TIMEOUT_SECONDS),
        TASK_ROOT,
        target.module,
        *target.entrypoints,
    ]


def _packaging_hint(target: HandlerTarget) -> str:
    """What the failed import could see, in the words of its packaging."""
    runtime = "the standard library and the runtime's SDK (boto3 and the packages it requires)"
    if target.packaging == "source":
        return (
            f"lambda/{target.name} deploys as a zip of its source directory, so only its own "
            f"files, {runtime} are importable; its requirements.txt is not installed."
        )
    return (
        f"lambda/{target.name} deploys lambda/{target.code_directory.name}, which the build "
        f"action stages (cli/stacks.py); only what it stages, {runtime} are importable."
    )


def import_target(
    target: HandlerTarget, *, cli: str, image: str, runner: Runner = run_command
) -> tuple[str | None, str]:
    """Import *target* in a fresh container: ``(failure or None, the check's report)``."""
    try:
        result = runner(
            import_command(target, cli=cli, image=image),
            timeout=CONTAINER_TIMEOUT_SECONDS,
            capture=True,
        )
    except subprocess.TimeoutExpired:
        detail = f"the container did not exit within {CONTAINER_TIMEOUT_SECONDS} s"
    else:
        if result.returncode == 0:
            return None, result.stdout.strip()
        detail = _excerpt(result.stderr) or f"exit {result.returncode}"
    return f"{target.label} failed:\n{detail}\n  {_packaging_hint(target)}", ""


def verify_handlers(
    targets: list[HandlerTarget], *, cli: str, image: str, runner: Runner = run_command
) -> list[str]:
    """Import every target, each in its own container; the failures, in order.

    The SDK each container reports is printed when it first appears (the
    image is the same for every target, so normally once).
    """
    failures: list[str] = []
    reported = ""
    for target in targets:
        print(f"=== {target.label} ===", flush=True)
        failure, report = import_target(target, cli=cli, image=image, runner=runner)
        if failure is not None:
            print(failure, flush=True)
            failures.append(failure)
        elif report != reported:
            print(report, flush=True)
            reported = report
    return failures


def _asset_consumer(root: Path) -> AbstractContextManager[None]:
    """Hold the build action's directories source-current while they are imported.

    ``cdk_asset_consumer`` is the CLI's entry point for anything that reads
    the generated ``*-build`` directories: it rebuilds a stale one first and
    keeps it unchanged until the block exits. It comes from this script's own
    checkout, whatever *root* is.
    """
    _use_this_checkout()
    from cli.stacks import cdk_asset_consumer

    return cdk_asset_consumer(root)


def main(
    argv: list[str] | None = None,
    *,
    runner: Runner = run_command,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=REPO_ROOT,
        help="Repository root (defaults to the checkout containing this script)",
    )
    args = parser.parse_args(argv)
    root = args.root.resolve()
    try:
        targets = discover_handlers(root)
        image = read_base_image(root)
        cli = container_cli()
        pull_image(cli, image, runner=runner, sleep=sleep)
        with _asset_consumer(root):
            failures = verify_handlers(targets, cli=cli, image=image, runner=runner)
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if failures:
        print(
            f"{len(failures)} of {len(targets)} Python Lambda handlers failed to import in {image}."
        )
        return 1
    print(f"Imported {len(targets)} Python Lambda handlers in {image}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
