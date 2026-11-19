"""Behavioral tests for CI import and container-version verification helpers."""

from __future__ import annotations

import ast
import importlib.util
import json
import os
import re
import shlex
import subprocess
import sys
import venv
from contextlib import nullcontext
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / ".github" / "scripts"
WORKFLOW = ROOT / ".github" / "workflows" / "integration-tests.yml"
IMAGE = "public.ecr.aws/lambda/python:3.14"
DIGEST = "sha256:" + "0123456789abcdef" * 4

#: Every zipped Python handler as the Lambda import verifier models it:
#: (source directory, packaging, code directory, module, entrypoints).
#: test_lambda_verifier_targets_match_the_stacks holds it to gco/stacks; the
#: two image Lambdas are imported inside their images by their
#: integration:docker:* jobs (test_every_image_lambda_is_imported_inside_its_built_image).
EXPECTED_TARGETS = {
    ("analytics-cleanup", "source", "lambda/analytics-cleanup", "handler", ("handler",)),
    (
        "analytics-presigned-url",
        "source",
        "lambda/analytics-presigned-url",
        "handler",
        ("lambda_handler",),
    ),
    ("api-gateway-proxy", "source", "lambda/api-gateway-proxy", "handler", ("lambda_handler",)),
    ("capacity-poller", "source", "lambda/capacity-poller", "handler", ("lambda_handler",)),
    (
        "cross-region-aggregator",
        "source",
        "lambda/cross-region-aggregator",
        "handler",
        ("lambda_handler",),
    ),
    ("drift-detection", "source", "lambda/drift-detection", "handler", ("lambda_handler",)),
    (
        "ga-registration",
        "source",
        "lambda/ga-registration",
        "handler",
        ("lambda_handler", "on_delete_event"),
    ),
    (
        "helm-installer",
        "source",
        "lambda/helm-installer",
        "teardown_provider",
        ("drain_install_executions", "is_complete", "on_event"),
    ),
    ("helm-orchestrator", "source", "lambda/helm-orchestrator", "handler", ("on_event",)),
    ("image-lookup", "source", "lambda/image-lookup", "handler", ("lambda_handler",)),
    (
        "kubectl-applier-simple",
        "build",
        "lambda/kubectl-applier-simple-build",
        "handler",
        ("lambda_handler",),
    ),
    ("pcs-lookup", "source", "lambda/pcs-lookup", "handler", ("lambda_handler",)),
    ("regional-api-proxy", "source", "lambda/regional-api-proxy", "handler", ("lambda_handler",)),
    ("secret-rotation", "source", "lambda/secret-rotation", "handler", ("lambda_handler",)),
    (
        "traffic-dial-controller",
        "source",
        "lambda/traffic-dial-controller",
        "handler",
        ("lambda_handler",),
    ),
    ("transaction-search", "source", "lambda/transaction-search", "handler", ("lambda_handler",)),
    ("vector-ingest", "source", "lambda/vector-ingest", "handler", ("lambda_handler",)),
}


def _load_script(name: str) -> ModuleType:
    path = SCRIPTS / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"_gco_test_{name}", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def lambda_verifier() -> ModuleType:
    return _load_script("verify_lambda_imports")


@pytest.fixture(scope="module")
def container_verifier() -> ModuleType:
    return _load_script("verify_container_tool_versions")


def _target(lambda_verifier: Any, directory: Path, **overrides: Any) -> Any:
    fields: dict[str, Any] = {
        "name": directory.name,
        "module": "handler",
        "entrypoints": ("lambda_handler",),
        "packaging": "source",
        "code_directory": directory,
    }
    fields.update(overrides)
    return lambda_verifier.HandlerTarget(**fields)


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _relative(path: Path) -> str:
    return path.relative_to(ROOT).as_posix()


# ---------------------------------------------------------------------------
# Lambda import verifier: what is deployed, and how
# ---------------------------------------------------------------------------


def test_lambda_verifier_discovers_every_zipped_handler(lambda_verifier: Any) -> None:
    targets = lambda_verifier.discover_handlers(ROOT)
    actual = {
        (
            target.name,
            target.packaging,
            _relative(target.code_directory),
            target.module,
            target.entrypoints,
        )
        for target in targets
    }
    assert actual == EXPECTED_TARGETS
    assert len(targets) == len(EXPECTED_TARGETS)


def _code_directory(node: ast.expr | None, assigned: dict[str, ast.expr]) -> str | None:
    """The ``lambda/...`` directory a ``code=`` expression packages."""
    if isinstance(node, ast.Name):
        node = assigned.get(node.id)
    if not isinstance(node, ast.Call):
        return None
    called = getattr(node.func, "attr", getattr(node.func, "id", ""))
    if called in {"lambda_source_code", "from_asset"} and node.args:
        first = node.args[0]
        return first.value if isinstance(first, ast.Constant) else None
    if called == "from_image_asset":
        # An image's CMD and ENTRYPOINT come from its Dockerfile; an override
        # here would name another handler, which this reader does not model.
        overrides = {keyword.arg for keyword in node.keywords} & {"cmd", "entrypoint"}
        assert not overrides, f"from_image_asset overrides {sorted(overrides)}"
        for keyword in node.keywords:
            if keyword.arg == "directory" and isinstance(keyword.value, ast.Constant):
                return str(keyword.value.value)
    return None


def _deployed_python_handlers() -> set[tuple[str, str, str]]:
    """``(packaging, code directory, module.function)`` of every Python Lambda in gco/stacks.

    Read from the source (no synth): each ``lambda_.Function`` (``zip``) names
    its ``handler`` and ``code``; a ``DockerImageFunction``'s (``image``)
    handler is its Dockerfile's ``CMD``. A ``code=`` variable resolves to its
    assignment in the same method, and a ``handler=`` name to a module-level
    string constant of the same file (``PRESIGNED_URL_HANDLER``, which the API
    document generator also reads).
    """
    deployed: set[tuple[str, str, str]] = set()
    for path in sorted((ROOT / "gco" / "stacks").glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        module_strings = {
            node.targets[0].id: node.value.value
            for node in tree.body
            if isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        }
        for method in ast.walk(tree):
            if not isinstance(method, ast.FunctionDef):
                continue
            assigned: dict[str, ast.expr] = {
                node.targets[0].id: node.value
                for node in ast.walk(method)
                if isinstance(node, ast.Assign)
                and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)
            }
            for node in ast.walk(method):
                if not (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr in {"Function", "DockerImageFunction"}
                ):
                    continue
                keywords = {keyword.arg: keyword.value for keyword in node.keywords}
                directory = _code_directory(keywords.get("code"), assigned)
                assert directory is not None, f"{path.name}:{node.lineno}: unresolved code="
                if "NODEJS" in ast.unparse(keywords.get("runtime", ast.Constant(""))):
                    continue
                handler = keywords.get("handler")
                if node.func.attr == "DockerImageFunction":
                    assert handler is None, f"{path.name}:{node.lineno}"
                    dockerfile = ROOT / directory.removesuffix("-build") / "Dockerfile"
                    command = re.search(r'^CMD \["([^"]+)"\]$', dockerfile.read_text(), re.M)
                    assert command is not None, dockerfile
                    deployed.add(("image", directory, command.group(1)))
                else:
                    if isinstance(handler, ast.Name):
                        value = module_strings.get(handler.id)
                    elif isinstance(handler, ast.Constant):
                        value = handler.value
                    else:
                        value = None
                    assert isinstance(value, str), f"{path.name}:{node.lineno}: handler="
                    deployed.add(("zip", directory, value))
    return deployed


def test_lambda_verifier_targets_match_the_stacks(lambda_verifier: Any) -> None:
    """Every zipped Python handler a stack deploys is imported, from the code it deploys.

    Discovery starts from ``lambda/*/handler.py``; this holds the verifier's
    tables (entrypoints, extra modules, build packaging) to the stacks, so a
    new function, a renamed handler or a packaging change cannot leave a
    deployed module unchecked. ``teardown_provider`` once was.
    """
    verified = {
        (_relative(target.code_directory), f"{target.module}.{entrypoint}")
        for target in lambda_verifier.discover_handlers(ROOT)
        for entrypoint in target.entrypoints
    }
    zipped = {
        (directory, handler)
        for packaging, directory, handler in _deployed_python_handlers()
        if packaging == "zip"
    }
    assert len(zipped) >= 20
    assert verified == zipped


#: The only form of in-image import step the contract below accepts (no
#: network, the Region Lambda sets); the tag and ``-I -B -c <program>`` follow.
_IN_IMAGE_IMPORT = (
    "docker",
    "run",
    "--rm",
    "--network",
    "none",
    "-e",
    "AWS_REGION=us-east-1",
    "-e",
    "AWS_DEFAULT_REGION=us-east-1",
    "--entrypoint",
    "python",
)


def _in_image_import_programs() -> dict[str, list[str]]:
    """Image build context -> the ``-c`` programs its job runs inside the image it builds.

    A step counts only as ``docker run --rm --network none -e AWS_REGION=...
    -e AWS_DEFAULT_REGION=... --entrypoint python <tag> -I -B -c <program>``
    with the tag the same job's image build gives that context.
    """
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    programs: dict[str, list[str]] = {}
    for job in workflow["jobs"].values():
        steps = job.get("steps") or []
        for build in steps:
            if build.get("uses") != "./.github/actions/build-image-with-retry":
                continue
            prefix = [*_IN_IMAGE_IMPORT, build["with"]["tags"], "-I", "-B", "-c"]
            found = programs.setdefault(build["with"]["context"], [])
            for step in steps:
                run = str(step.get("run") or "").strip()
                if not run.startswith("docker run "):
                    continue
                words = shlex.split(run.replace("\\\n", " "))
                if words[: len(prefix)] == prefix and len(words) == len(prefix) + 1:
                    found.append(words[-1])
    return programs


def _run_in_image_program(program: str, task_root: Path) -> subprocess.CompletedProcess[str]:
    """Run a step's program as the image would, *task_root* standing for ``/var/task``."""
    task_root.mkdir(parents=True, exist_ok=True)
    return subprocess.run(
        [sys.executable, "-I", "-B", "-c", program],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
        cwd=task_root,
        env={**os.environ, "LAMBDA_TASK_ROOT": str(task_root)},
    )


def test_every_image_lambda_is_imported_inside_its_built_image(tmp_path: Path) -> None:
    """The verifier skips the image Lambdas, so their jobs must import them.

    For every ``DockerImageFunction`` in gco/stacks, the job that builds its
    image runs exactly one in-image import step. Its program is run here
    against stand-in task roots: it passes when the handler module defines
    every entrypoint, and fails when any one is missing or the module is not
    in the task root. An image Lambda added without such a step, or a step
    that stops checking an entrypoint, fails here.
    """
    images: dict[str, set[str]] = {}
    for packaging, directory, handler in _deployed_python_handlers():
        if packaging == "image":
            images.setdefault(directory.removesuffix("-build"), set()).add(handler)
    assert set(images) == {"lambda/helm-installer", "lambda/tls-certificate-manager"}
    programs = _in_image_import_programs()
    for context, handlers in sorted(images.items()):
        steps = programs.get(context, [])
        assert len(steps) == 1, f"{context}: {len(steps)} in-image import steps, expected 1"
        assert len(handlers) == 1, f"{context}: one image, one CMD: {sorted(handlers)}"
        module, _, entrypoint = next(iter(handlers)).rpartition(".")
        root = tmp_path / context
        _write(root / "complete" / f"{module}.py", f"def {entrypoint}(event, context): ...\n")
        passed = _run_in_image_program(steps[0], root / "complete")
        assert passed.returncode == 0, passed.stderr
        _write(root / "missing" / f"{module}.py", "")
        assert _run_in_image_program(steps[0], root / "missing").returncode != 0
        _write(root / "not-callable" / f"{module}.py", f"{entrypoint} = None\n")
        assert _run_in_image_program(steps[0], root / "not-callable").returncode != 0
        assert _run_in_image_program(steps[0], root / "empty").returncode != 0


def test_image_lambdas_build_on_the_lambda_python_runtime(lambda_verifier: Any) -> None:
    """The base image the verifier reads is the runtime the stacks deploy on.

    The verifier reads every ``lambda/*/Dockerfile``, which must be exactly the
    image Lambdas' Dockerfiles, and they must build FROM the Lambda Python
    base image for ``LAMBDA_PYTHON_RUNTIME`` (``PYTHON_3_14`` ->
    ``public.ecr.aws/lambda/python:3.14``, optionally ``@sha256:<digest>``),
    so the zipped handlers are imported on the runtime they deploy on.
    """
    constants = (ROOT / "gco" / "stacks" / "constants.py").read_text(encoding="utf-8")
    runtime = re.search(r'^LAMBDA_PYTHON_RUNTIME = "PYTHON_(\d+)_(\d+)"$', constants, re.M)
    assert runtime is not None
    version = f"{runtime.group(1)}.{runtime.group(2)}"
    images = {
        f"{directory.removesuffix('-build')}/Dockerfile"
        for packaging, directory, _handler in _deployed_python_handlers()
        if packaging == "image"
    }
    assert {_relative(path) for path in lambda_verifier.image_dockerfiles(ROOT)} == images
    reference = lambda_verifier.read_base_image(ROOT)
    assert re.fullmatch(
        rf"public\.ecr\.aws/lambda/python:{re.escape(version)}(@sha256:[0-9a-f]{{64}})?",
        reference,
    ), reference
    # The handlers' unit tests and integration:pytest:cross-module import them
    # under .python-version, so it must be the Python they deploy on.
    assert (ROOT / ".python-version").read_text(encoding="utf-8").strip() == version


def test_lambda_verifier_refuses_a_root_with_no_handlers(
    lambda_verifier: Any, tmp_path: Path
) -> None:
    """Discovering nothing must fail, not report success over an empty list.

    The job's whole value is "every handler still imports". If a refactor moved
    ``lambda/`` and discovery silently returned no targets, the step would print
    a cheerful zero and pass while checking nothing.
    """
    (tmp_path / "lambda").mkdir()
    with pytest.raises(RuntimeError, match="no Python Lambda handlers found"):
        lambda_verifier.discover_handlers(tmp_path)


def _empty_tables(monkeypatch: pytest.MonkeyPatch, lambda_verifier: Any) -> None:
    """The real tables name handlers in this repository, absent from a synthetic root."""
    for name in ("ENTRYPOINT_OVERRIDES", "EXTRA_SOURCE_MODULES", "BUILD_DIRECTORIES"):
        monkeypatch.setattr(lambda_verifier, name, {})


@pytest.mark.parametrize(
    "table", ["ENTRYPOINT_OVERRIDES", "EXTRA_SOURCE_MODULES", "BUILD_DIRECTORIES"]
)
def test_lambda_verifier_refuses_settings_naming_a_missing_handler(
    lambda_verifier: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, table: str
) -> None:
    """A stale table entry is a silent hole, so it fails loudly.

    Each table describes how a named Lambda deploys. If the Lambda is renamed
    or deleted and its entry is left behind, the entry stops applying to
    anything, and the handler it described would be checked the wrong way.
    """
    _write(tmp_path / "lambda" / "real-handler" / "handler.py", "def lambda_handler(): ...\n")
    _empty_tables(monkeypatch, lambda_verifier)
    monkeypatch.setitem(getattr(lambda_verifier, table), "handler-that-moved", "anything")
    with pytest.raises(
        RuntimeError, match="settings reference missing handlers: handler-that-moved"
    ):
        lambda_verifier.discover_handlers(tmp_path)


def test_an_image_lambda_handler_is_left_to_its_image_but_its_zipped_modules_are_not(
    lambda_verifier: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A directory with a Dockerfile is an image; only its extra zipped modules are targets."""
    _write(tmp_path / "lambda" / "imaged" / "handler.py", "")
    _write(tmp_path / "lambda" / "imaged" / "Dockerfile", f"FROM {IMAGE}\n")
    _write(tmp_path / "lambda" / "imaged" / "cleanup.py", "")
    _write(tmp_path / "lambda" / "zipped" / "handler.py", "")
    _empty_tables(monkeypatch, lambda_verifier)
    monkeypatch.setitem(lambda_verifier.EXTRA_SOURCE_MODULES, "imaged", {"cleanup": ("on_event",)})
    targets = lambda_verifier.discover_handlers(tmp_path)
    assert [
        (target.name, target.module, target.entrypoints, target.packaging) for target in targets
    ] == [
        ("imaged", "cleanup", ("on_event",), "source"),
        ("zipped", "handler", ("lambda_handler",), "source"),
    ]


# ---------------------------------------------------------------------------
# Lambda import verifier: the base image
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "reference",
    [IMAGE, f"{IMAGE}@{DIGEST}", "public.ecr.aws/lambda/python:3.14.2026.11.19.10"],
)
def test_the_base_image_is_read_from_the_image_dockerfiles(
    lambda_verifier: Any, tmp_path: Path, reference: str
) -> None:
    """A digest (or a dated tag) pinned in the Dockerfiles is what the imports run on."""
    _write(
        tmp_path / "lambda" / "one" / "Dockerfile",
        f'# FROM in a comment is not an instruction\nFROM {reference}\nCMD ["handler.x"]\n',
    )
    # Instructions are case-insensitive.
    _write(tmp_path / "lambda" / "two" / "Dockerfile", f"from {reference}  \n")
    # A generated copy is not a source, even when it is stale.
    _write(tmp_path / "lambda" / "one-build" / "Dockerfile", "FROM python:3.13\n")
    assert lambda_verifier.read_base_image(tmp_path) == reference


@pytest.mark.parametrize(
    ("dockerfiles", "message"),
    [
        pytest.param({}, "no lambda/*/Dockerfile under", id="no-dockerfile"),
        pytest.param(
            {"one": 'CMD ["handler.x"]\n'}, "lambda/one/Dockerfile has no FROM line", id="no-from"
        ),
        pytest.param(
            {"one": "FROM python:3.14-slim\n"},
            "lambda/one/Dockerfile: FROM python:3.14-slim is not the Lambda Python base image",
            id="another-image",
        ),
        pytest.param(
            {"one": "FROM public.ecr.aws/lambda/nodejs:24\n"},
            "is not the Lambda Python base image",
            id="another-runtime",
        ),
        pytest.param(
            {"one": "FROM public.ecr.aws/lambda/python\n"},
            "is not the Lambda Python base image",
            id="no-tag",
        ),
        pytest.param(
            {"one": f"FROM {IMAGE}@sha256:abc\n"},
            "is not the Lambda Python base image",
            id="short-digest",
        ),
        pytest.param(
            {"one": f"FROM --platform=linux/amd64 {IMAGE}\n"},
            "is not the Lambda Python base image",
            id="flag",
        ),
        pytest.param(
            {"one": f"FROM {IMAGE} AS base\n"}, "is not the Lambda Python base image", id="stage"
        ),
        pytest.param(
            {"one": f"ARG BASE={IMAGE}\nFROM ${{BASE}}\n"},
            "FROM ${BASE} is not the Lambda Python base image",
            id="argument",
        ),
        pytest.param(
            {"one": f"FROM {IMAGE}\n", "two": "FROM public.ecr.aws/lambda/python:3.13\n"},
            "disagree on the base image: public.ecr.aws/lambda/python:3.14 in "
            "lambda/one/Dockerfile; public.ecr.aws/lambda/python:3.13 in lambda/two/Dockerfile",
            id="disagree",
        ),
        pytest.param(
            {"one": f"FROM {IMAGE}\nFROM {IMAGE}@{DIGEST}\n"},
            "disagree on the base image",
            id="disagree-within-a-file",
        ),
    ],
)
def test_the_base_image_must_be_one_lambda_python_reference(
    lambda_verifier: Any, tmp_path: Path, dockerfiles: dict[str, str], message: str
) -> None:
    (tmp_path / "lambda").mkdir()
    for name, text in dockerfiles.items():
        _write(tmp_path / "lambda" / name / "Dockerfile", text)
    with pytest.raises(RuntimeError, match=re.escape(message)):
        lambda_verifier.read_base_image(tmp_path)


# ---------------------------------------------------------------------------
# Lambda import verifier: the check program, run for real against a fake runtime
# ---------------------------------------------------------------------------

#: boto3's requirements in the fake runtime, shaped like the image's.
BOTO3_REQUIRES = (
    "botocore (<1.44.0,>=1.43.0)",
    "jmespath (<2.0.0,>=0.7.1)",
    "botocore[crt] (<2.0a0,>=1.21.0) ; extra == 'crt'",
)
#: What the default fake runtime gives a handler, and what it refuses.
FAKE_SDK = "boto3 botocore jmespath python-dateutil six urllib3"
FAKE_REFUSED = (
    "awscrt awslambdaric legacy_backport nsonly pip preloaded runtime_client simplejson stray"
)


def _distribution(
    site_packages: Path,
    name: str,
    files: dict[str, str],
    *,
    requires: tuple[str, ...] = (),
    top_level: tuple[str, ...] = (),
) -> None:
    """Install a fake distribution: its files, METADATA and RECORD.

    ``importlib.metadata`` reads a distribution's modules from ``top_level.txt``
    when there is one and otherwise infers them from RECORD, as for a wheel
    built without setuptools (urllib3's).
    """
    info = f"{name.replace('-', '_')}-1.0.dist-info"
    for relative, text in files.items():
        _write(site_packages / relative, text)
    metadata = ["Metadata-Version: 2.1", f"Name: {name}", "Version: 1.0"]
    metadata += [f"Requires-Dist: {requirement}" for requirement in requires]
    _write(site_packages / info / "METADATA", "\n".join(metadata) + "\n")
    if top_level:
        _write(site_packages / info / "top_level.txt", "\n".join(top_level) + "\n")
    record = [*files, f"{info}/METADATA", f"{info}/RECORD"]
    _write(site_packages / info / "RECORD", "".join(f"{entry},,\n" for entry in record))


def _fake_runtime(
    directory: Path,
    *,
    boto3_requires: tuple[str, ...] = BOTO3_REQUIRES,
    distributions: tuple[str, ...] = (),
) -> Path:
    """A stand-in for the base image's interpreter; returns its ``python``.

    A stdlib-only virtualenv whose site-packages is laid out like the image's:
    the SDK (boto3 and what it requires), packages only an extra or a false
    marker reaches (``awscrt``, ``legacy-backport``), the runtime interface
    client with its dependency (``awslambdaric``, ``runtime_client``,
    ``simplejson``), pip, a module no distribution owns, a namespace package
    and a module a ``.pth`` file imports at interpreter start.
    ``distributions`` adds one empty single-module distribution per name.
    """
    venv.EnvBuilder(with_pip=False, symlinks=True).create(directory)
    site_packages = next((directory / "lib").glob("python*/site-packages"))
    _distribution(
        site_packages,
        "boto3",
        {"boto3/__init__.py": ""},
        requires=boto3_requires,
        top_level=("boto3",),
    )
    _distribution(
        site_packages,
        "botocore",
        {"botocore/__init__.py": ""},
        requires=(
            "jmespath (<2.0.0,>=0.7.1)",
            "python-dateutil (<3.0.0,>=2.1)",
            "urllib3 (!=2.2.0,<3,>=1.25.4) ; python_version >= '3.10'",
            # False on 3.14, where comparing strings ("3.14" < "3.8") would say true.
            "legacy-backport ; python_version < '3.8'",
            "awscrt (==0.38.0) ; extra == 'crt'",
        ),
        top_level=("botocore",),
    )
    _distribution(site_packages, "jmespath", {"jmespath/__init__.py": ""}, top_level=("jmespath",))
    _distribution(
        site_packages,
        "python-dateutil",
        {"dateutil/__init__.py": ""},
        requires=("six >=1.5",),
        top_level=("dateutil",),
    )
    _distribution(site_packages, "six", {"six.py": ""})
    _distribution(
        site_packages,
        "urllib3",
        {"urllib3/__init__.py": ""},
        requires=(
            "brotli>=1.2.0; (platform_python_implementation == 'CPython') and extra == 'brotli'",
        ),
    )
    _distribution(site_packages, "awscrt", {"awscrt/__init__.py": ""})
    _distribution(site_packages, "legacy-backport", {"legacy_backport.py": ""})
    _distribution(
        site_packages,
        "awslambdaric",
        {"awslambdaric/__init__.py": "", "runtime_client.py": ""},
        requires=("simplejson>=3.20.1",),
        top_level=("awslambdaric", "runtime_client"),
    )
    _distribution(site_packages, "simplejson", {"simplejson/__init__.py": "SHIPPED = False\n"})
    _distribution(site_packages, "pip", {"pip/__init__.py": ""})
    for name in distributions:
        _distribution(site_packages, name, {f"{name.replace('-', '_')}.py": ""})
    _write(site_packages / "stray.py", "")
    _write(site_packages / "nsonly" / "part.py", "")
    _write(site_packages / "preloaded.py", "")
    _write(site_packages / "preload.pth", "import preloaded\n")
    return directory / "bin" / "python"


@pytest.fixture(scope="module")
def fake_runtime(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return _fake_runtime(tmp_path_factory.mktemp("runtime"))


def _check(
    lambda_verifier: Any,
    python: Path,
    task_root: Path,
    *,
    module: str = "handler",
    entrypoints: tuple[str, ...] = ("lambda_handler",),
    timeout: int = 30,
    environment: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run the real check program as the container does, *python* as the image's interpreter."""
    task_root.mkdir(parents=True, exist_ok=True)
    return subprocess.run(
        [
            str(python),
            "-I",
            "-B",
            "-c",
            lambda_verifier.CHECK_PROGRAM,
            str(timeout),
            str(task_root),
            module,
            *entrypoints,
        ],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
        cwd=task_root,
        env=environment,
    )


def _summary(result: subprocess.CompletedProcess[str]) -> tuple[str, str]:
    """``(SDK, refused)`` from the check program's success line."""
    assert result.returncode == 0, result.stderr
    found = re.fullmatch(
        r"Runtime SDK: (.*)\. Refused unless the deployment ships them: (.*)\.\n", result.stdout
    )
    assert found is not None, result.stdout
    return found.group(1), found.group(2)


def test_the_check_allows_the_runtime_sdk_and_reports_what_it_refuses(
    lambda_verifier: Any, fake_runtime: Path, tmp_path: Path
) -> None:
    _write(tmp_path / "shared.py", "VALUE = 1\n")
    _write(
        tmp_path / "handler.py",
        "import json\n"
        "import boto3, botocore, dateutil, jmespath, six, urllib3\n"
        "import shared\n\n"
        "def lambda_handler(event, context): ...\n",
    )
    assert _summary(_check(lambda_verifier, fake_runtime, tmp_path)) == (FAKE_SDK, FAKE_REFUSED)


@pytest.mark.parametrize(
    "module",
    [
        pytest.param("awscrt", id="reached-only-through-an-extra"),
        pytest.param("legacy_backport", id="reached-only-through-a-false-marker"),
        pytest.param("simplejson", id="the-interface-client's-dependency"),
        pytest.param("awslambdaric", id="the-runtime-interface-client"),
        pytest.param("runtime_client", id="a-second-module-of-a-distribution"),
        pytest.param("pip", id="pip"),
        pytest.param("stray", id="a-module-no-distribution-owns"),
        pytest.param("nsonly", id="a-namespace-package"),
        pytest.param("preloaded", id="imported-at-interpreter-start"),
    ],
)
def test_the_runtime_s_other_modules_are_refused(
    lambda_verifier: Any, fake_runtime: Path, tmp_path: Path, module: str
) -> None:
    """The image has them, but only the SDK is the runtime's promise to handler code."""
    _write(tmp_path / "handler.py", f"import {module}\n\ndef lambda_handler(): ...\n")
    result = _check(lambda_verifier, fake_runtime, tmp_path)
    assert result.returncode == 1
    assert f"No module named '{module}': the Lambda runtime's site-packages has it" in result.stderr
    assert f"only the runtime's SDK ({FAKE_SDK})" in result.stderr


@pytest.mark.parametrize(
    ("files", "module"),
    [
        pytest.param({"simplejson/__init__.py": "SHIPPED = True\n"}, "simplejson", id="package"),
        pytest.param({"runtime_client.py": "SHIPPED = True\n"}, "runtime_client", id="module"),
    ],
)
def test_a_module_the_task_root_ships_is_imported_from_the_task_root(
    lambda_verifier: Any, fake_runtime: Path, tmp_path: Path, files: dict[str, str], module: str
) -> None:
    for relative, text in files.items():
        _write(tmp_path / relative, text)
    _write(
        tmp_path / "handler.py",
        f"import {module}\nassert {module}.SHIPPED\n\ndef lambda_handler(): ...\n",
    )
    assert _check(lambda_verifier, fake_runtime, tmp_path).returncode == 0


def test_a_namespace_directory_in_the_task_root_does_not_unblock_a_module(
    lambda_verifier: Any, fake_runtime: Path, tmp_path: Path
) -> None:
    """A bare ``simplejson/`` would let the image's regular package win the import."""
    _write(tmp_path / "simplejson" / "notes.txt", "")
    _write(tmp_path / "handler.py", "import simplejson\n\ndef lambda_handler(): ...\n")
    result = _check(lambda_verifier, fake_runtime, tmp_path)
    assert result.returncode == 1
    assert "No module named 'simplejson': the Lambda runtime's site-packages has it" in (
        result.stderr
    )


def test_pythonpath_stays_out_of_the_import(
    lambda_verifier: Any, fake_runtime: Path, tmp_path: Path
) -> None:
    """Imports used to resolve against the project's environment.

    PyYAML is reachable through PYTHONPATH here, yet a zipped Lambda that
    imports it would fail in AWS, so the check fails too.
    """
    shadow = _write(tmp_path / "shadow" / "yaml" / "__init__.py", "")
    task_root = tmp_path / "needs-yaml"
    _write(task_root / "handler.py", "import yaml\n\ndef lambda_handler(): ...\n")
    _write(task_root / "requirements.txt", "PyYAML==6.0.3\n")
    environment = {**os.environ, "PYTHONPATH": str(shadow.parent.parent)}
    result = _check(lambda_verifier, fake_runtime, task_root, environment=environment)
    assert result.returncode == 1
    assert "No module named 'yaml'" in result.stderr
    assert "site-packages has it" not in result.stderr


def test_every_entrypoint_must_be_callable(
    lambda_verifier: Any, fake_runtime: Path, tmp_path: Path
) -> None:
    _write(tmp_path / "handler.py", "def lambda_handler(): ...\n\non_delete_event = None\n")
    result = _check(
        lambda_verifier,
        fake_runtime,
        tmp_path,
        entrypoints=("lambda_handler", "on_delete_event"),
    )
    assert result.returncode == 1
    assert "not callable: on_delete_event" in result.stderr


def test_the_task_root_pth_files_are_processed_as_the_bootstrap_does(
    lambda_verifier: Any, fake_runtime: Path, tmp_path: Path
) -> None:
    """``/var/runtime/bootstrap.py`` calls ``site.addsitedir(LAMBDA_TASK_ROOT)``."""
    _write(tmp_path / "vendor.pth", "vendor\n")
    _write(tmp_path / "vendor" / "vendored.py", "")
    _write(tmp_path / "handler.py", "import vendored\n\ndef lambda_handler(): ...\n")
    assert _check(lambda_verifier, fake_runtime, tmp_path).returncode == 0


def test_an_empty_task_root_is_reported_as_not_mounted(
    lambda_verifier: Any, fake_runtime: Path, tmp_path: Path
) -> None:
    """A VM-backed runtime shows a path it does not share as an empty directory."""
    result = _check(lambda_verifier, fake_runtime, tmp_path / "unshared")
    assert result.returncode == 1
    assert "is empty: the code directory was not mounted" in result.stderr


def test_an_import_that_hangs_is_stopped_and_reported_where_it_hung(
    lambda_verifier: Any,
    fake_runtime: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The watchdog's stack dump starts with the innermost frames, so they are kept."""
    monkeypatch.setattr(lambda_verifier, "IMPORT_TIMEOUT_SECONDS", 1)
    _write(tmp_path / "handler.py", "import time\n\ntime.sleep(60)\n")
    failure, _report = lambda_verifier.import_target(
        _target(lambda_verifier, tmp_path),
        cli="docker",
        image=IMAGE,
        runner=_LocalContainer(fake_runtime),
    )
    assert (
        "failed:\nthe import did not finish within 1 s; the stack when it was stopped:\n"
        "Timeout (0:00:01)!\n"
    ) in failure
    assert 'handler.py", line 3' in failure


def test_requirement_markers_are_evaluated_for_the_interpreter_with_no_extras(
    lambda_verifier: Any, tmp_path: Path
) -> None:
    """PEP 508 markers: release numbers compare as versions, ``extra`` is empty."""
    allowed = {
        "numeric-version": "python_version >= '3.9'",
        "full-version": "python_full_version > '3.0.0' and implementation_version >= '3'",
        "either-os": "sys_platform != 'win32' and (os_name == 'posix' or os_name == 'nt')",
        "listed-platform": "sys_platform in 'linux darwin'",
        "cpython": "implementation_name == 'cpython' and platform_python_implementation == 'CPython'",
        "padded-release": f"python_version == '{sys.version_info.major}.{sys.version_info.minor}.0'",
    }
    refused = {
        "old-python": "python_version < '3.9'",
        "unlisted-platform": "sys_platform not in 'linux darwin'",
        "reversed-extra": "'crt' == extra",
        "any-extra": "extra == 'a' or extra == 'b'",
    }
    python = _fake_runtime(
        tmp_path / "runtime",
        boto3_requires=tuple(
            f"{name} ; {marker}" for name, marker in {**allowed, **refused}.items()
        ),
        distributions=(*allowed, *refused),
    )
    _write(tmp_path / "task" / "handler.py", "def lambda_handler(): ...\n")
    sdk, blocked = _summary(_check(lambda_verifier, python, tmp_path / "task"))
    assert sdk.split() == sorted(["boto3", *allowed])
    assert {name.replace("-", "_") for name in refused} <= set(blocked.split())


@pytest.mark.parametrize(
    ("requirement", "message"),
    [
        pytest.param("bad ; python_version ~= '3.1'", "python_version ~= '3.1'", id="syntax"),
        pytest.param("bad ; unknown_variable == 'x'", "unknown_variable == 'x'", id="variable"),
        pytest.param("bad ; python_version is '3'", "python_version is '3'", id="operator"),
        pytest.param("bad ; os_name < 'posix'", "os_name < 'posix'", id="ordering-strings"),
        pytest.param("bad ; '3' < python_version < '4'", "'3' < python_version < '4'", id="chain"),
        pytest.param("bad ; not extra", "not extra", id="negation"),
        pytest.param("bad ; python_version == 3", "python_version == 3", id="number"),
    ],
)
def test_a_marker_the_check_cannot_evaluate_is_an_error(
    lambda_verifier: Any, tmp_path: Path, requirement: str, message: str
) -> None:
    """Guessing would either widen the SDK or refuse a module the runtime gives."""
    python = _fake_runtime(tmp_path / "runtime", boto3_requires=(requirement,))
    _write(tmp_path / "task" / "handler.py", "def lambda_handler(): ...\n")
    result = _check(lambda_verifier, python, tmp_path / "task")
    assert result.returncode == 1
    assert "cannot evaluate the environment marker" in result.stderr
    assert message in result.stderr


@pytest.mark.parametrize(
    ("requirement", "message"),
    [
        pytest.param(
            "[not-a-requirement]",
            "cannot parse the requirement '[not-a-requirement]' of boto3",
            id="unparseable",
        ),
        pytest.param(
            "not-installed",
            "No package metadata was found for not-installed",
            id="not-installed",
        ),
    ],
)
def test_a_requirement_the_check_cannot_follow_is_an_error(
    lambda_verifier: Any, tmp_path: Path, requirement: str, message: str
) -> None:
    python = _fake_runtime(tmp_path / "runtime", boto3_requires=(requirement,))
    _write(tmp_path / "task" / "handler.py", "def lambda_handler(): ...\n")
    result = _check(lambda_verifier, python, tmp_path / "task")
    assert result.returncode == 1
    assert message in result.stderr


# ---------------------------------------------------------------------------
# Lambda import verifier: the containers, with the runner injected
# ---------------------------------------------------------------------------


class _LocalContainer:
    """Stands in for the container CLI: ``pull`` succeeds, ``run`` runs locally.

    ``run`` checks the isolation flags, then runs the command after the image
    with *python* as the image's interpreter, the mounted directory standing
    for ``/var/task`` and the ``-e`` variables set.
    """

    def __init__(self, python: Path) -> None:
        self.python = python
        self.commands: list[list[str]] = []

    def __call__(
        self, command: list[str], *, timeout: float, capture: bool
    ) -> subprocess.CompletedProcess[str]:
        self.commands.append(list(command))
        if command[1] == "pull":
            return subprocess.CompletedProcess(command, 0)
        image_at = command.index("--entrypoint") + 2
        options = command[2:image_at]
        isolation = ["--rm", "--platform", "linux/amd64", "--pull", "never", "--network", "none"]
        assert options[: len(isolation)] == isolation
        source, task_root, mode = options[options.index("-v") + 1].rsplit(":", 2)
        assert (task_root, mode) == ("/var/task", "ro")
        environment = dict(os.environ)
        for index, word in enumerate(options):
            if word == "-e":
                key, _, value = options[index + 1].partition("=")
                environment[key] = value
        arguments = [source if word == task_root else word for word in command[image_at + 1 :]]
        return subprocess.run(
            [str(self.python), *arguments],
            capture_output=capture,
            text=True,
            timeout=timeout,
            check=False,
            cwd=source,
            env=environment,
        )


class _Scripted:
    """A runner answering from a script: ``(returncode, stdout, stderr)`` or an exception."""

    def __init__(self, *outcomes: tuple[int, str, str] | BaseException) -> None:
        self.outcomes = list(outcomes)
        self.calls: list[tuple[list[str], float, bool]] = []

    def __call__(
        self, command: list[str], *, timeout: float, capture: bool
    ) -> subprocess.CompletedProcess[str]:
        self.calls.append((list(command), timeout, capture))
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        returncode, stdout, stderr = outcome
        return subprocess.CompletedProcess(command, returncode, stdout, stderr)


def test_each_import_runs_in_a_read_only_offline_container_of_the_image(
    lambda_verifier: Any, tmp_path: Path
) -> None:
    target = _target(lambda_verifier, tmp_path, entrypoints=("lambda_handler", "on_event"))
    assert lambda_verifier.import_command(target, cli="finch", image=IMAGE) == [
        "finch",
        "run",
        "--rm",
        "--platform",
        "linux/amd64",
        "--pull",
        "never",
        "--network",
        "none",
        "-v",
        f"{tmp_path}:/var/task:ro",
        "-e",
        "AWS_REGION=us-east-1",
        "-e",
        "AWS_DEFAULT_REGION=us-east-1",
        "--entrypoint",
        "/var/lang/bin/python3",
        IMAGE,
        "-I",
        "-B",
        "-c",
        lambda_verifier.CHECK_PROGRAM,
        "30",
        "/var/task",
        "handler",
        "lambda_handler",
        "on_event",
    ]


def test_verify_handlers_imports_each_target_in_its_own_container(
    lambda_verifier: Any, fake_runtime: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    good = tmp_path / "lambda" / "good"
    _write(good / "handler.py", "import boto3\n\ndef lambda_handler(): ...\n")
    bad = tmp_path / "lambda" / "bad"
    _write(bad / "handler.py", "import simplejson\n\ndef lambda_handler(): ...\n")
    build = tmp_path / "lambda" / "built-build"
    _write(build / "vendored" / "__init__.py", "")
    _write(
        build / "handler.py",
        "import os\nimport vendored\n\n"
        "assert os.environ['AWS_REGION'] == 'us-east-1'\n\n"
        "def lambda_handler(): ...\n",
    )
    targets = [
        _target(lambda_verifier, good),
        _target(lambda_verifier, bad),
        _target(lambda_verifier, build, name="built", packaging="build"),
    ]
    runner = _LocalContainer(fake_runtime)
    failures = lambda_verifier.verify_handlers(targets, cli="docker", image=IMAGE, runner=runner)
    output = capsys.readouterr().out
    assert [failure.split(" failed:")[0] for failure in failures] == [
        "lambda/bad handler.lambda_handler (source)"
    ]
    assert "No module named 'simplejson'" in failures[0]
    assert "deploys as a zip of its source directory" in failures[0]
    assert "=== lambda/good handler.lambda_handler (source) ===" in output
    assert "=== lambda/built handler.lambda_handler (build) ===" in output
    # Two containers reported the same SDK; it is printed once.
    assert output.count(f"Runtime SDK: {FAKE_SDK}.") == 1
    assert [command[command.index("-v") + 1] for command in runner.commands] == [
        f"{good}:/var/task:ro",
        f"{bad}:/var/task:ro",
        f"{build}:/var/task:ro",
    ]


def test_verify_handlers_prints_each_new_sdk_report(
    lambda_verifier: Any, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    runner = _Scripted((0, "Runtime SDK: a.\n", ""), (0, "Runtime SDK: a.\n", ""), (0, "b\n", ""))
    targets = [_target(lambda_verifier, tmp_path / name) for name in ("one", "two", "three")]
    assert lambda_verifier.verify_handlers(targets, cli="docker", image=IMAGE, runner=runner) == []
    output = capsys.readouterr().out
    assert output.count("Runtime SDK: a.") == 1
    assert output.count("\nb\n") == 1
    assert [timeout for _command, timeout, _capture in runner.calls] == [90, 90, 90]
    assert all(capture for _command, _timeout, capture in runner.calls)


@pytest.mark.parametrize(
    ("packaging", "hint"),
    [
        ("source", "deploys as a zip of its source directory, so only its own files"),
        ("build", "deploys lambda/applier-build, which the build action stages (cli/stacks.py)"),
    ],
)
def test_a_failed_import_reports_the_tail_of_its_output_and_the_packaging(
    lambda_verifier: Any, tmp_path: Path, packaging: str, hint: str
) -> None:
    target = _target(
        lambda_verifier, tmp_path / "applier-build", name="applier", packaging=packaging
    )
    stderr = "".join(f"line {number}\n" for number in range(1, 21))
    failure, report = lambda_verifier.import_target(
        target, cli="docker", image=IMAGE, runner=_Scripted((1, "", stderr))
    )
    assert report == ""
    assert failure.startswith(f"lambda/applier handler.lambda_handler ({packaging}) failed:\n")
    assert "line 5\n" not in failure
    assert "line 6\n" in failure
    assert "line 20\n" in failure
    assert hint in failure
    assert "the runtime's SDK (boto3 and the packages it requires)" in failure


def test_a_failure_without_output_reports_the_exit_status(
    lambda_verifier: Any, tmp_path: Path
) -> None:
    failure, _report = lambda_verifier.import_target(
        _target(lambda_verifier, tmp_path),
        cli="docker",
        image=IMAGE,
        runner=_Scripted((125, "", "")),
    )
    assert "failed:\nexit 125\n" in failure


def test_a_container_that_does_not_exit_is_a_failure(lambda_verifier: Any, tmp_path: Path) -> None:
    runner = _Scripted(subprocess.TimeoutExpired(["docker"], 90))
    failure, _report = lambda_verifier.import_target(
        _target(lambda_verifier, tmp_path), cli="docker", image=IMAGE, runner=runner
    )
    assert "the container did not exit within 90 s" in failure


def test_the_image_is_pulled_once_for_the_lambda_platform(
    lambda_verifier: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    runner = _Scripted((0, "", ""))
    lambda_verifier.pull_image("docker", IMAGE, runner=runner, sleep=pytest.fail)
    assert runner.calls == [(["docker", "pull", "--platform", "linux/amd64", IMAGE], 300, False)]
    assert f"$ docker pull --platform linux/amd64 {IMAGE}  (attempt 1/5)" in capsys.readouterr().out


def test_a_refused_pull_is_retried(
    lambda_verifier: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    sleeps: list[float] = []
    runner = _Scripted((1, "", ""), subprocess.TimeoutExpired(["docker"], 300), (0, "", ""))
    lambda_verifier.pull_image("docker", IMAGE, runner=runner, sleep=sleeps.append)
    assert sleeps == [15.0, 15.0]
    output = capsys.readouterr().out
    assert "Pull failed (exit 1); retrying in 15 s." in output
    assert "Pull failed (no result within 300 s); retrying in 15 s." in output


def test_a_pull_that_keeps_failing_names_the_image(lambda_verifier: Any) -> None:
    sleeps: list[float] = []
    runner = _Scripted(*[(1, "", "")] * 5)
    with pytest.raises(
        RuntimeError,
        match=re.escape(
            f"could not pull {IMAGE} (linux/amd64) with docker: the last of 5 attempts "
            "ended with exit 1"
        ),
    ):
        lambda_verifier.pull_image("docker", IMAGE, runner=runner, sleep=sleeps.append)
    assert len(sleeps) == 4


def test_a_container_cli_that_cannot_run_is_named(lambda_verifier: Any) -> None:
    runner = _Scripted(FileNotFoundError(2, "No such file or directory"))
    with pytest.raises(RuntimeError, match="cannot run the container CLI 'nope'"):
        lambda_verifier.pull_image("nope", IMAGE, runner=runner, sleep=pytest.fail)


def test_run_command_captures_or_streams_and_is_bounded(
    lambda_verifier: Any, capfd: pytest.CaptureFixture[str]
) -> None:
    captured = lambda_verifier.run_command(
        [sys.executable, "-c", "print('captured')"], timeout=60, capture=True
    )
    assert (captured.returncode, captured.stdout) == (0, "captured\n")
    streamed = lambda_verifier.run_command(
        [sys.executable, "-c", "print('streamed')"], timeout=60, capture=False
    )
    assert streamed.stdout is None
    assert "streamed" in capfd.readouterr().out
    with pytest.raises(subprocess.TimeoutExpired):
        lambda_verifier.run_command(
            [sys.executable, "-c", "import time; time.sleep(30)"], timeout=0.5, capture=True
        )


def test_the_container_cli_is_chosen_as_the_cdk_deploy_chooses_it(
    lambda_verifier: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``CDK_DOCKER`` first (the convention for finch), then a running docker, finch, podman."""
    import cli._container_runtime as runtime

    monkeypatch.setattr(runtime, "_container_runtime_cache", runtime._UNCHECKED)
    monkeypatch.setenv("CDK_DOCKER", "/opt/custom/finch")
    assert lambda_verifier.container_cli() == "/opt/custom/finch"
    monkeypatch.setattr(runtime, "detect_container_runtime", lambda: None)
    monkeypatch.setattr(runtime.shutil, "which", lambda name: None)
    with pytest.raises(RuntimeError, match="No container runtime found") as raised:
        lambda_verifier.container_cli()
    assert "CDK_DOCKER" in str(raised.value)


def test_the_asset_consumer_comes_from_this_checkout(
    lambda_verifier: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A root without generated assets has nothing to hold or rebuild."""
    monkeypatch.setattr(sys, "path", [entry for entry in sys.path if entry != str(ROOT)])
    with lambda_verifier._asset_consumer(tmp_path):
        assert sys.path[0] == str(ROOT)
    with lambda_verifier._asset_consumer(tmp_path):
        assert sys.path.count(str(ROOT)) == 1


# ---------------------------------------------------------------------------
# Lambda import verifier: main
# ---------------------------------------------------------------------------


def _patch_main(monkeypatch: pytest.MonkeyPatch, lambda_verifier: Any) -> None:
    monkeypatch.setattr(lambda_verifier, "container_cli", lambda: "docker")
    monkeypatch.setattr(lambda_verifier, "_asset_consumer", lambda root: nullcontext())


def test_lambda_verifier_main_defaults_to_this_checkout(
    lambda_verifier: Any, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """``main`` with no arguments imports every target of the repository it ships in."""
    _patch_main(monkeypatch, lambda_verifier)
    image = lambda_verifier.read_base_image(ROOT)
    runner = _Scripted(*[(0, "Runtime SDK: boto3.\n", "")] * (1 + len(EXPECTED_TARGETS)))
    assert lambda_verifier.main([], runner=runner, sleep=pytest.fail) == 0
    commands = [command for command, _timeout, _capture in runner.calls]
    assert commands[0] == ["docker", "pull", "--platform", "linux/amd64", image]
    assert len(commands) == 1 + len(EXPECTED_TARGETS)
    assert all(command[1] == "run" and image in command for command in commands[1:])
    output = capsys.readouterr().out
    assert f"Imported {len(EXPECTED_TARGETS)} Python Lambda handlers in {image}." in output


def test_lambda_verifier_main_accepts_an_explicit_root(
    lambda_verifier: Any,
    fake_runtime: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _write(tmp_path / "lambda" / "only-handler" / "handler.py", "def lambda_handler(): ...\n")
    _write(tmp_path / "lambda" / "imaged" / "handler.py", "import missing_everywhere\n")
    _write(tmp_path / "lambda" / "imaged" / "Dockerfile", f"FROM {IMAGE}@{DIGEST}\n")
    _empty_tables(monkeypatch, lambda_verifier)
    _patch_main(monkeypatch, lambda_verifier)
    runner = _LocalContainer(fake_runtime)
    assert lambda_verifier.main(["--root", str(tmp_path)], runner=runner, sleep=pytest.fail) == 0
    assert len(runner.commands) == 2
    output = capsys.readouterr().out
    assert f"Imported 1 Python Lambda handlers in {IMAGE}@{DIGEST}." in output


def test_lambda_verifier_main_reports_failed_imports(
    lambda_verifier: Any, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _patch_main(monkeypatch, lambda_verifier)
    image = lambda_verifier.read_base_image(ROOT)
    outcomes = [(0, "", ""), (1, "", "Traceback ...")] + [(0, "ok\n", "")] * (
        len(EXPECTED_TARGETS) - 1
    )
    assert lambda_verifier.main([], runner=_Scripted(*outcomes), sleep=pytest.fail) == 1
    output = capsys.readouterr().out
    assert f"1 of {len(EXPECTED_TARGETS)} Python Lambda handlers failed to import in {image}." in (
        output
    )


@pytest.mark.parametrize("failure", ["no-dockerfile", "no-container-cli"])
def test_lambda_verifier_main_reports_what_stops_it_before_any_import(
    lambda_verifier: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    failure: str,
) -> None:
    _write(tmp_path / "lambda" / "only-handler" / "handler.py", "def lambda_handler(): ...\n")
    _empty_tables(monkeypatch, lambda_verifier)
    _patch_main(monkeypatch, lambda_verifier)
    expected = "no lambda/*/Dockerfile under"
    if failure == "no-container-cli":
        _write(tmp_path / "lambda" / "imaged" / "Dockerfile", f"FROM {IMAGE}\n")
        expected = "No container runtime is running"

        def no_cli() -> str:
            raise RuntimeError(expected)

        monkeypatch.setattr(lambda_verifier, "container_cli", no_cli)
    runner = _Scripted()
    assert lambda_verifier.main(["--root", str(tmp_path)], runner=runner, sleep=pytest.fail) == 2
    assert runner.calls == []
    assert f"error: {expected}" in capsys.readouterr().err


def _dev_outputs() -> dict[tuple[str, ...], str]:
    return {
        ("node", "--version"): "v24.22.2",
        ("npm", "--version"): "12.2.1",
        ("cdk", "--version"): "2.1147.0 (build abc123)",
        ("aws", "--version"): "aws-cli/2.39.3 Python/3.13.11 Linux/6.11",
        ("docker", "--version"): "Docker version 29.9.2, build deadbeef",
        ("docker", "buildx", "version"): "github.com/docker/buildx v0.38.1 abc123",
        ("uv", "--version"): "uv 0.13.6 (abc123 2026-10-27)",
        ("uvx", "--version"): "uvx 0.13.6 (abc123 2026-10-27)",
        ("session-manager-plugin", "--version"): "1.2.896.0",
        (
            "kubectl",
            "version",
            "--client=true",
            "--output=json",
        ): json.dumps({"clientVersion": {"gitVersion": "v1.37.2"}}),
    }


def _dev_runner(outputs: dict[tuple[str, ...], str]):
    def run(command: list[str]) -> str:
        assert command[:4] == ["docker", "run", "--rm", "gco-dev"]
        return outputs[tuple(command[4:])]

    return run


def test_dev_verifier_accepts_only_matching_runtime_versions(container_verifier: Any) -> None:
    actual = container_verifier.verify_dev_image(
        "gco-dev",
        ROOT / "Dockerfile.dev",
        runner=_dev_runner(_dev_outputs()),
    )

    assert actual == {
        "Node.js": "v24.22.2",
        "npm": "12.2.1",
        "CDK": "2.1147.0",
        "AWS CLI": "2.39.3",
        "Docker CLI": "29.9.2",
        "Buildx": "v0.38.1",
        "uv": "0.13.6",
        "uvx": "0.13.6",
        "Session Manager plugin": "1.2.896.0",
        "kubectl": "v1.37.2",
    }


def test_dev_verifier_rejects_a_valid_but_wrong_runtime_version(
    container_verifier: Any,
) -> None:
    outputs = _dev_outputs()
    outputs[("node", "--version")] = "v24.18.0"

    with pytest.raises(container_verifier.VerificationError, match=re.escape("expected Node.js")):
        container_verifier.verify_dev_image(
            "gco-dev",
            ROOT / "Dockerfile.dev",
            runner=_dev_runner(outputs),
        )


def _helm_runner(*, kubectl_version: str = "v1.37.2"):
    outputs = {
        (
            "docker",
            "run",
            "--rm",
            "--entrypoint",
            "helm",
            "helm-installer:ci",
            "version",
            "--short",
        ): "v4.3.1+gabcdef",
        (
            "docker",
            "run",
            "--rm",
            "--entrypoint",
            "kubectl",
            "helm-installer:ci",
            "version",
            "--client=true",
            "--output=json",
        ): json.dumps({"clientVersion": {"gitVersion": kubectl_version}}),
    }

    def run(command: list[str]) -> str:
        return outputs[tuple(command)]

    return run


def test_helm_installer_verifier_accepts_matching_runtime_versions(
    container_verifier: Any,
) -> None:
    actual = container_verifier.verify_helm_installer_image(
        "helm-installer:ci",
        ROOT / "lambda" / "helm-installer" / "Dockerfile",
        runner=_helm_runner(),
    )

    assert actual == {"Helm": "v4.3.1", "kubectl": "v1.37.2"}


def test_helm_installer_verifier_rejects_a_mismatched_runtime_version(
    container_verifier: Any,
) -> None:
    with pytest.raises(container_verifier.VerificationError, match="expected kubectl"):
        container_verifier.verify_helm_installer_image(
            "helm-installer:ci",
            ROOT / "lambda" / "helm-installer" / "Dockerfile",
            runner=_helm_runner(kubectl_version="v1.37.0"),
        )


def test_container_verifier_refuses_a_dockerfile_missing_a_pin(
    container_verifier: Any, tmp_path: Path
) -> None:
    """An absent ARG must fail, not compare against an empty expectation.

    ``_require_version`` compares strings; a missing pin would otherwise become
    ``""`` and the check would report a mismatch against nothing, or worse,
    silently accept whatever the image happened to ship.
    """
    dockerfile = tmp_path / "Dockerfile.dev"
    dockerfile.write_text("FROM scratch\nARG NODE_VERSION=24.21.0\n", encoding="utf-8")

    with pytest.raises(
        container_verifier.VerificationError, match=re.escape("missing Dockerfile.dev pins")
    ):
        container_verifier.parse_dev_pins(dockerfile)


@pytest.mark.parametrize(
    ("content", "pattern", "expected_error"),
    [
        pytest.param(
            "RUN install helm 4.2.4\nRUN install helm 4.2.5\n",
            r"helm (\d+\.\d+\.\d+)",
            "expected one Helm pin, found: 4.2.4, 4.2.5",
            id="two-disagreeing-pins",
        ),
        pytest.param(
            "FROM scratch\n",
            r"helm (\d+\.\d+\.\d+)",
            "expected one Helm pin, found: none",
            id="no-pin-at-all",
        ),
        pytest.param(
            "RUN install helm 4.2.4\n",
            r"(helm) (\d+\.\d+\.\d+)",
            "exactly one capture group",
            id="pattern-with-two-groups",
        ),
    ],
)
def test_container_verifier_requires_exactly_one_resolvable_pin(
    container_verifier: Any, content: str, pattern: str, expected_error: str
) -> None:
    """Two pins that disagree is the dangerous case: neither is authoritative."""
    with pytest.raises(container_verifier.VerificationError, match=re.escape(expected_error)):
        container_verifier._single_release_pin(content, pattern, "Helm")


def test_container_verifier_rejects_unparseable_tool_output(container_verifier: Any) -> None:
    with pytest.raises(
        container_verifier.VerificationError, match=re.escape("could not parse Node.js")
    ):
        container_verifier._extract_version("command not found", r"v(\d+\.\d+\.\d+)", "Node.js")


@pytest.mark.parametrize(
    ("output", "expected_error"),
    [
        pytest.param("not json at all", "could not parse kubectl", id="not-json"),
        pytest.param('{"clientVersion": {}}', "could not parse kubectl", id="missing-key"),
        pytest.param('{"clientVersion": {"gitVersion": 12}}', "invalid kubectl", id="not-a-string"),
        pytest.param(
            '{"clientVersion": {"gitVersion": "1.36"}}', "invalid kubectl", id="wrong-shape"
        ),
    ],
)
def test_container_verifier_validates_the_kubectl_version_document(
    container_verifier: Any, output: str, expected_error: str
) -> None:
    """kubectl reports JSON, so a shape change must fail rather than coerce."""
    with pytest.raises(container_verifier.VerificationError, match=expected_error):
        container_verifier._kubectl_version(output)


def test_container_verifier_run_command_returns_stderr_when_stdout_is_empty(
    container_verifier: Any,
) -> None:
    """Several tools print their version to stderr; both streams are accepted."""
    output = container_verifier.run_command(
        [sys.executable, "-c", "import sys; print('v1.2.3', file=sys.stderr)"]
    )

    assert output == "v1.2.3"


def test_container_verifier_run_command_refuses_silent_output(container_verifier: Any) -> None:
    """A command that exits 0 saying nothing has told us nothing."""
    with pytest.raises(container_verifier.VerificationError, match="produced no version output"):
        container_verifier.run_command([sys.executable, "-c", ""])


@pytest.mark.parametrize("profile", ["dev", "helm-installer"])
def test_container_verifier_main_dispatches_each_profile(
    container_verifier: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, profile: str
) -> None:
    """Both profiles must reach their own verifier with the resolved Dockerfile."""
    seen: dict[str, object] = {}
    monkeypatch.setattr(
        container_verifier,
        "verify_dev_image",
        lambda image, dockerfile: seen.update(profile="dev", image=image, dockerfile=dockerfile),
    )
    monkeypatch.setattr(
        container_verifier,
        "verify_helm_installer_image",
        lambda image, dockerfile: seen.update(profile="helm", image=image, dockerfile=dockerfile),
    )
    dockerfile = tmp_path / "Dockerfile"
    dockerfile.write_text("FROM scratch\n", encoding="utf-8")

    exit_code = container_verifier.main(
        [profile, "--image", "some-image:ci", "--dockerfile", str(dockerfile)]
    )

    assert exit_code == 0
    assert seen["image"] == "some-image:ci"
    assert seen["dockerfile"] == dockerfile
    assert seen["profile"] == ("dev" if profile == "dev" else "helm")


@pytest.mark.parametrize(
    ("profile", "expected_suffix"),
    [
        ("dev", "Dockerfile.dev"),
        ("helm-installer", str(Path("lambda") / "helm-installer" / "Dockerfile")),
    ],
)
def test_container_verifier_main_defaults_the_dockerfile_per_profile(
    container_verifier: Any, monkeypatch: pytest.MonkeyPatch, profile: str, expected_suffix: str
) -> None:
    """Without --dockerfile each profile resolves its own committed one."""
    resolved: list[Path] = []
    for name in ("verify_dev_image", "verify_helm_installer_image"):
        monkeypatch.setattr(
            container_verifier, name, lambda image, dockerfile: resolved.append(dockerfile)
        )

    assert container_verifier.main([profile, "--image", "x:ci"]) == 0
    assert str(resolved[0]).endswith(expected_suffix)
    assert resolved[0].is_file(), f"{resolved[0]} is not a committed file"


def test_lambda_mypy_inventory_covers_every_python_package() -> None:
    """The Lambda mypy loop must discover every authored Python package."""
    import re

    import yaml

    workflow_path = ROOT / ".github" / "workflows" / "lint.yml"
    workflow = yaml.safe_load(workflow_path.read_text(encoding="utf-8"))
    job = workflow["jobs"]["lint-mypy-lambda"]
    step = next(item for item in job["steps"] if item.get("name") == "Run mypy on each Lambda dir")
    configured = set(re.findall(r"\blambda/([a-z0-9-]+)\b", step["run"]))

    lambda_root = ROOT / "lambda"
    actual = {
        path.name
        for path in lambda_root.iterdir()
        if path.is_dir() and not path.name.endswith("-build") and any(path.glob("*.py"))
    }

    assert configured == actual, (
        "lint:mypy:lambda inventory drifted; "
        f"missing={sorted(actual - configured)!r}, "
        f"stale={sorted(configured - actual)!r}"
    )
