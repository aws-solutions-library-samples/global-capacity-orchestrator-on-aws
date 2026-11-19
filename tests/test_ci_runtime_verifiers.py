"""Behavioral tests for CI import and container-version verification helpers."""

from __future__ import annotations

import ast
import importlib.util
import json
import re
import subprocess
import sys
import venv
from contextlib import nullcontext
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / ".github" / "scripts"

#: Every deployed Python handler as the Lambda import verifier models it:
#: (source directory, packaging, code directory, module, entrypoints).
#: test_lambda_verifier_targets_match_the_stacks holds it to gco/stacks.
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
    ("helm-installer", "image", "lambda/helm-installer-build", "handler", ("lambda_handler",)),
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
        "tls-certificate-manager",
        "image",
        "lambda/tls-certificate-manager",
        "handler",
        ("lambda_handler",),
    ),
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

#: What the Lambda Python runtime ships for handler code. Adding a name here
#: (to make an import pass) would claim the runtime ships it; a package the
#: runtime lacks belongs in the Lambda's own deployment instead.
RUNTIME_PACKAGE_NAMES = {
    "boto3",
    "botocore",
    "jmespath",
    "python-dateutil",
    "s3transfer",
    "six",
    "urllib3",
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


@pytest.fixture(scope="module")
def empty_runtime(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """An interpreter with no third-party packages at all (stdlib venv, no pip).

    Stands in for the runtime virtualenv uv builds, offline: whatever a child
    import finds must come from the paths the verifier hands it.
    """
    directory = tmp_path_factory.mktemp("runtime")
    venv.EnvBuilder(with_pip=False, symlinks=True).create(directory)
    return directory / "bin" / "python"


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


def test_lambda_verifier_discovers_every_deployed_handler(lambda_verifier: Any) -> None:
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
    images = {target.name: target.task_files for target in targets if target.packaging == "image"}
    assert images == {
        "helm-installer": ("requirements.txt", "handler.py", "charts.yaml"),
        "tls-certificate-manager": ("requirements.txt", "handler.py"),
    }


def _code_directory(node: ast.expr | None, assigned: dict[str, str]) -> str | None:
    """The ``lambda/...`` directory a ``code=`` expression packages."""
    if isinstance(node, ast.Name):
        return assigned.get(node.id)
    if not isinstance(node, ast.Call):
        return None
    called = getattr(node.func, "attr", getattr(node.func, "id", ""))
    if called in {"lambda_source_code", "from_asset"} and node.args:
        first = node.args[0]
        return first.value if isinstance(first, ast.Constant) else None
    if called == "from_image_asset":
        for keyword in node.keywords:
            if keyword.arg == "directory" and isinstance(keyword.value, ast.Constant):
                return str(keyword.value.value)
    return None


def _deployed_python_handlers() -> set[tuple[str, str]]:
    """``(code directory, module.function)`` of every Python Lambda in gco/stacks.

    Read from the source (no synth): each ``lambda_.Function`` names its
    ``handler`` and ``code``; a ``DockerImageFunction``'s handler is its
    Dockerfile's ``CMD``. A ``code=`` variable resolves to its assignment in
    the same method.
    """
    deployed: set[tuple[str, str]] = set()
    for path in sorted((ROOT / "gco" / "stacks").glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for method in ast.walk(tree):
            if not isinstance(method, ast.FunctionDef):
                continue
            assigned: dict[str, str] = {}
            for node in ast.walk(method):
                if (
                    isinstance(node, ast.Assign)
                    and len(node.targets) == 1
                    and isinstance(node.targets[0], ast.Name)
                ):
                    directory = _code_directory(node.value, {})
                    if directory is not None:
                        assigned[node.targets[0].id] = directory
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
                if handler is None:
                    dockerfile = ROOT / directory.removesuffix("-build") / "Dockerfile"
                    command = re.search(r'^CMD \["([^"]+)"\]$', dockerfile.read_text(), re.M)
                    assert command is not None, dockerfile
                    deployed.add((directory, command.group(1)))
                else:
                    assert isinstance(handler, ast.Constant), f"{path.name}:{node.lineno}"
                    deployed.add((directory, str(handler.value)))
    return deployed


def test_lambda_verifier_targets_match_the_stacks(lambda_verifier: Any) -> None:
    """Every Python handler a stack deploys is imported, from the code it deploys.

    Discovery starts from ``lambda/*/handler.py``; this holds the verifier's
    tables (entrypoints, extra modules, build and image packaging) to the
    stacks, so a new function, a renamed handler or a packaging change cannot
    leave a deployed module unchecked. ``teardown_provider`` once was.
    """
    verified = {
        (_relative(target.code_directory), f"{target.module}.{entrypoint}")
        for target in lambda_verifier.discover_handlers(ROOT)
        for entrypoint in target.entrypoints
    }
    deployed = _deployed_python_handlers()
    assert len(deployed) >= 20
    assert verified == deployed


def test_runtime_packages_pin_what_the_lambda_runtime_ships(lambda_verifier: Any) -> None:
    packages = lambda_verifier.load_runtime_packages(ROOT / lambda_verifier.RUNTIME_PACKAGES)
    constants = (ROOT / "gco" / "stacks" / "constants.py").read_text(encoding="utf-8")
    runtime = re.search(r'^LAMBDA_PYTHON_RUNTIME = "PYTHON_(\d+)_(\d+)"$', constants, re.M)
    assert runtime is not None
    # A runtime bump must come with the new runtime's package set.
    assert packages.runtime == f"python{runtime.group(1)}.{runtime.group(2)}"
    # The CI job runs the check under .python-version, which must be that runtime.
    python_version = (ROOT / ".python-version").read_text(encoding="utf-8").strip()
    assert packages.runtime == f"python{python_version}"
    assert set(packages.pins) == RUNTIME_PACKAGE_NAMES
    assert packages.pins["boto3"] == packages.pins["botocore"]


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("boto3==1.0.0\nbotocore==1.0.0\n", "no '# lambda-runtime: pythonX.Y' line"),
        ("# lambda-runtime: python3.14\nboto3>=1.0\n", "'boto3>=1.0' is not an exact"),
        ("# lambda-runtime: python3.14\nboto3==1.0.0\n", "no pin for botocore"),
    ],
)
def test_runtime_packages_refuse_a_malformed_file(
    lambda_verifier: Any, tmp_path: Path, text: str, message: str
) -> None:
    path = _write(tmp_path / "runtime.txt", text)
    with pytest.raises(RuntimeError, match=re.escape(message)):
        lambda_verifier.load_runtime_packages(path)


def test_runtime_packages_read_names_case_insensitively(
    lambda_verifier: Any, tmp_path: Path
) -> None:
    path = _write(
        tmp_path / "runtime.txt",
        "# comment\n\n#  lambda-runtime:  python3.14 \nBoto3==1.2.3\nbotocore==1.2.3\n",
    )
    packages = lambda_verifier.load_runtime_packages(path)
    assert (packages.runtime, packages.pins) == (
        "python3.14",
        {"boto3": "1.2.3", "botocore": "1.2.3"},
    )


_IMAGE_HEAD = "FROM public.ecr.aws/lambda/python:3.14\nWORKDIR ${LAMBDA_TASK_ROOT}\n"


def test_image_plan_reads_the_task_root_files_and_handler(
    lambda_verifier: Any, tmp_path: Path
) -> None:
    dockerfile = _write(
        tmp_path / "Dockerfile",
        _IMAGE_HEAD + "# a comment\n"
        "COPY requirements.txt .\n"
        "RUN pip install \\\n"
        "    # comment inside a continuation\n"
        "    --target ${LAMBDA_TASK_ROOT} -r requirements.txt\n"
        "COPY handler.py helpers/ ./\n"
        "COPY data.json ${LAMBDA_TASK_ROOT}/\n"
        'CMD ["app.main"]\n',
    )
    assert lambda_verifier.read_image_plan(dockerfile) == (
        ("requirements.txt", "handler.py", "helpers/", "data.json"),
        "app",
        "main",
    )


@pytest.mark.parametrize(
    ("body", "message"),
    [
        ('ADD handler.py .\nCMD ["handler.lambda_handler"]\n', "unsupported ADD"),
        (
            'COPY --chown=1000 handler.py .\nCMD ["handler.lambda_handler"]\n',
            "unsupported COPY",
        ),
        ('COPY *.py .\nCMD ["handler.lambda_handler"]\n', "unsupported COPY"),
        ('COPY handler.py /opt/\nCMD ["handler.lambda_handler"]\n', "unsupported COPY"),
        ('WORKDIR /opt\nCOPY handler.py .\nCMD ["handler.lambda_handler"]\n', "unsupported COPY"),
        ('COPY ["handler.py", "."]\nCMD ["handler.lambda_handler"]\n', "unsupported COPY"),
        ("COPY handler.py .\n", "no CMD names the handler"),
        ("CMD handler.lambda_handler\n", "CMD must be"),
        ('CMD ["handler.lambda_handler", "extra"]\n', "CMD must be"),
        ('CMD ["handler"]\n', "CMD must be"),
        ("CMD [7]\n", "CMD must be"),
        ('FROM scratch\nCMD ["handler.lambda_handler"]\n', "exactly one FROM"),
        ('CMD ["handler.lambda_handler"]\nRUN true \\\n', "line continuation"),
    ],
)
def test_image_plan_refuses_forms_it_cannot_model(
    lambda_verifier: Any, tmp_path: Path, body: str, message: str
) -> None:
    dockerfile = _write(tmp_path / "Dockerfile", _IMAGE_HEAD + body)
    with pytest.raises(RuntimeError, match=re.escape(message)):
        lambda_verifier.read_image_plan(dockerfile)


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


@pytest.mark.parametrize(
    "table",
    ["ENTRYPOINT_OVERRIDES", "EXTRA_SOURCE_MODULES", "BUILD_DIRECTORIES", "IMAGE_CONTEXTS"],
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
    for name in ("ENTRYPOINT_OVERRIDES", "EXTRA_SOURCE_MODULES", "BUILD_DIRECTORIES"):
        monkeypatch.setattr(lambda_verifier, name, {})
    monkeypatch.setattr(lambda_verifier, "IMAGE_CONTEXTS", {})
    monkeypatch.setitem(getattr(lambda_verifier, table), "handler-that-moved", "anything")
    with pytest.raises(
        RuntimeError, match="settings reference missing handlers: handler-that-moved"
    ):
        lambda_verifier.discover_handlers(tmp_path)


# ---------------------------------------------------------------------------
# Lambda import verifier: the environments
# ---------------------------------------------------------------------------


def test_a_source_lambda_cannot_import_what_its_zip_does_not_ship(
    lambda_verifier: Any, empty_runtime: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The finding: imports used to resolve against the project's environment.

    PyYAML is installed here (the project needs it) and reachable through
    PYTHONPATH, yet a zipped Lambda that imports it would fail in AWS, so the
    check must fail too.
    """
    shadow = _write(tmp_path / "shadow" / "yaml" / "__init__.py", "")
    monkeypatch.setenv("PYTHONPATH", str(shadow.parent.parent))
    directory = tmp_path / "lambda" / "needs-yaml"
    _write(directory / "handler.py", "import yaml\n\ndef lambda_handler(): ...\n")
    _write(directory / "requirements.txt", "PyYAML==6.0.3\n")
    target = _target(lambda_verifier, directory)
    failure = lambda_verifier.import_target(target, python=empty_runtime, paths=[directory])
    assert failure is not None
    assert "No module named 'yaml'" in failure
    assert "deploys as a zip of its source directory" in failure
    assert "its requirements.txt is not installed" in failure


def test_a_handler_imports_from_its_own_files(
    lambda_verifier: Any, empty_runtime: Path, tmp_path: Path
) -> None:
    directory = tmp_path / "lambda" / "self-contained"
    _write(directory / "shared.py", "VALUE = 1\n")
    _write(directory / "handler.py", "import shared\n\ndef lambda_handler(): ...\n")
    target = _target(lambda_verifier, directory)
    assert lambda_verifier.import_target(target, python=empty_runtime, paths=[directory]) is None


def test_every_entrypoint_must_be_callable(
    lambda_verifier: Any, empty_runtime: Path, tmp_path: Path
) -> None:
    directory = tmp_path / "lambda" / "half-wired"
    _write(directory / "handler.py", "def lambda_handler(): ...\n\non_delete_event = None\n")
    target = _target(lambda_verifier, directory, entrypoints=("lambda_handler", "on_delete_event"))
    failure = lambda_verifier.import_target(target, python=empty_runtime, paths=[directory])
    assert failure is not None
    assert "not callable: on_delete_event" in failure


def test_a_failure_without_output_reports_the_exit_status(
    lambda_verifier: Any, empty_runtime: Path, tmp_path: Path
) -> None:
    directory = tmp_path / "lambda" / "silent"
    _write(directory / "handler.py", "import os\nos._exit(3)\n")
    target = _target(lambda_verifier, directory)
    failure = lambda_verifier.import_target(target, python=empty_runtime, paths=[directory])
    assert failure is not None
    assert "exit 3" in failure


def test_a_build_lambda_imports_what_the_build_staged(
    lambda_verifier: Any, empty_runtime: Path, tmp_path: Path
) -> None:
    build = tmp_path / "lambda" / "applier-build"
    _write(build / "vendored" / "__init__.py", "")
    _write(build / "handler.py", "import vendored\n\ndef lambda_handler(): ...\n")
    target = _target(lambda_verifier, build, name="applier", packaging="build")
    assert lambda_verifier.stage_target(
        target, uv="uv", python=empty_runtime, scratch=tmp_path
    ) == [build]
    assert lambda_verifier.import_target(target, python=empty_runtime, paths=[build]) is None
    (build / "vendored" / "__init__.py").unlink()
    (build / "vendored").rmdir()
    failure = lambda_verifier.import_target(target, python=empty_runtime, paths=[build])
    assert failure is not None
    assert "deploys lambda/applier-build, which the build action stages" in failure


def test_a_missing_staged_directory_names_the_build_action(
    lambda_verifier: Any, empty_runtime: Path, tmp_path: Path
) -> None:
    target = _target(lambda_verifier, tmp_path / "lambda" / "never-built-build", packaging="build")
    with pytest.raises(RuntimeError, match="stage it first with the build action"):
        lambda_verifier.stage_target(target, uv="uv", python=empty_runtime, scratch=tmp_path)


def _fake_uv_installs(
    monkeypatch: pytest.MonkeyPatch, lambda_verifier: Any
) -> list[tuple[str, ...]]:
    """Record uv calls; a ``--target`` install writes one package per requirement."""
    calls: list[tuple[str, ...]] = []

    def run_uv(uv: str, *arguments: str) -> None:
        calls.append((uv, *arguments))
        if "--target" in arguments:
            target = Path(arguments[arguments.index("--target") + 1])
            requirements = Path(arguments[arguments.index("--requirement") + 1])
            for line in requirements.read_text(encoding="utf-8").splitlines():
                package = line.split("==")[0].strip().lower()
                if package:
                    _write(target / package / "__init__.py", "")

    monkeypatch.setattr(lambda_verifier, "_run_uv", run_uv)
    return calls


def test_an_image_task_root_holds_its_copied_files_and_requirements(
    lambda_verifier: Any, empty_runtime: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _fake_uv_installs(monkeypatch, lambda_verifier)
    context = tmp_path / "lambda" / "manager"
    _write(context / "requirements.txt", "cryptography==50.0.0\n")
    _write(
        context / "handler.py",
        "import cryptography\nimport helpers.util\n\ndef lambda_handler(): ...\n",
    )
    _write(context / "helpers" / "__init__.py", "")
    _write(context / "helpers" / "util.py", "")
    _write(context / "not_copied.py", "")
    target = _target(
        lambda_verifier,
        context,
        packaging="image",
        task_files=("requirements.txt", "handler.py", "helpers"),
    )
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    paths = lambda_verifier.stage_target(target, uv="uv", python=empty_runtime, scratch=scratch)
    task_root = scratch / "task-manager"
    assert paths == [task_root]
    assert sorted(path.name for path in task_root.iterdir()) == [
        "cryptography",
        "handler.py",
        "helpers",
        "requirements.txt",
    ]
    assert calls == [
        (
            "uv",
            "pip",
            "install",
            "--no-config",
            "--python",
            str(empty_runtime),
            "--target",
            str(task_root),
            "--requirement",
            str(task_root / "requirements.txt"),
        )
    ]
    assert lambda_verifier.import_target(target, python=empty_runtime, paths=paths) is None
    # A module the Dockerfile does not copy is not in the image.
    _write(task_root / "handler.py", "import not_copied\n\ndef lambda_handler(): ...\n")
    failure = lambda_verifier.import_target(target, python=empty_runtime, paths=paths)
    assert failure is not None
    assert "task root holds only requirements.txt, handler.py, helpers" in failure


def test_an_image_without_requirements_installs_nothing(
    lambda_verifier: Any, empty_runtime: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _fake_uv_installs(monkeypatch, lambda_verifier)
    context = tmp_path / "lambda" / "bare"
    _write(context / "handler.py", "def lambda_handler(): ...\n")
    target = _target(lambda_verifier, context, packaging="image", task_files=("handler.py",))
    paths = lambda_verifier.stage_target(target, uv="uv", python=empty_runtime, scratch=tmp_path)
    assert calls == []
    assert lambda_verifier.import_target(target, python=empty_runtime, paths=paths) is None


def test_the_runtime_environment_is_the_pinned_packages_only(
    lambda_verifier: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _fake_uv_installs(monkeypatch, lambda_verifier)
    packages = lambda_verifier.load_runtime_packages(ROOT / lambda_verifier.RUNTIME_PACKAGES)
    python = lambda_verifier.build_runtime_environment("uv", tmp_path / "runtime", packages)
    assert python == tmp_path / "runtime" / "bin" / "python"
    assert calls == [
        (
            "uv",
            "venv",
            "--no-config",
            "--quiet",
            "--python",
            sys.executable,
            str(tmp_path / "runtime"),
        ),
        (
            "uv",
            "pip",
            "install",
            "--no-config",
            "--python",
            str(python),
            "--no-deps",
            "--requirement",
            str(packages.path),
        ),
    ]


def test_uv_runs_bounded_and_checked(lambda_verifier: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    def run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        seen.update(command=command, **kwargs)
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(lambda_verifier.subprocess, "run", run)
    lambda_verifier._run_uv("/opt/uv", "pip", "list")
    assert seen == {
        "command": ["/opt/uv", "pip", "list"],
        "check": True,
        "timeout": lambda_verifier.UV_TIMEOUT_SECONDS,
    }


@pytest.mark.parametrize(
    ("system", "machine", "expected"),
    [
        ("linux", "x86_64", True),
        ("linux", "AMD64", True),
        ("linux", "aarch64", False),
        ("darwin", "x86_64", False),
    ],
)
def test_staged_wheels_load_only_on_linux_x86_64(
    lambda_verifier: Any,
    monkeypatch: pytest.MonkeyPatch,
    system: str,
    machine: str,
    expected: bool,
) -> None:
    monkeypatch.setattr(lambda_verifier.sys, "platform", system)
    monkeypatch.setattr(lambda_verifier.platform, "machine", lambda: machine)
    assert lambda_verifier.host_loads_staged_wheels() is expected


@pytest.mark.parametrize("loads_wheels", [True, False])
def test_verify_handlers_imports_each_target_and_skips_unloadable_builds(
    lambda_verifier: Any,
    empty_runtime: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    loads_wheels: bool,
) -> None:
    monkeypatch.setattr(
        lambda_verifier, "build_runtime_environment", lambda uv, directory, packages: empty_runtime
    )
    monkeypatch.setattr(lambda_verifier, "host_loads_staged_wheels", lambda: loads_wheels)
    good = tmp_path / "lambda" / "good"
    _write(good / "handler.py", "def lambda_handler(): ...\n")
    bad = tmp_path / "lambda" / "bad"
    _write(bad / "handler.py", "import yaml\n")
    build = tmp_path / "lambda" / "built-build"
    _write(build / "handler.py", "def lambda_handler(): ...\n")
    targets = [
        _target(lambda_verifier, good),
        _target(lambda_verifier, bad),
        _target(lambda_verifier, build, name="built", packaging="build"),
    ]
    outcome = lambda_verifier.verify_handlers(targets, uv="uv", packages=None)
    output = capsys.readouterr().out
    assert [failure.split(" failed:")[0] for failure in outcome.failures] == [
        "lambda/bad handler.lambda_handler (source)"
    ]
    assert "=== lambda/good handler.lambda_handler (source) ===" in output
    if loads_wheels:
        assert outcome.skipped == []
    else:
        assert len(outcome.skipped) == 1
        assert "holds wheels for Linux x86_64" in outcome.skipped[0]
        assert outcome.skipped[0] in output


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


def _patch_main(
    monkeypatch: pytest.MonkeyPatch,
    lambda_verifier: Any,
    *,
    failures: list[str] | None = None,
    skipped: list[str] | None = None,
) -> list[list[Any]]:
    seen: list[list[Any]] = []

    def verify(targets: list[Any], *, uv: str, packages: Any) -> Any:
        seen.append(targets)
        return lambda_verifier.Outcome(failures=failures or [], skipped=skipped or [])

    monkeypatch.setattr(lambda_verifier, "verify_handlers", verify)
    monkeypatch.setattr(lambda_verifier, "_asset_consumer", lambda root: nullcontext())
    monkeypatch.setattr(lambda_verifier.shutil, "which", lambda name: f"/usr/bin/{name}")
    return seen


def test_lambda_verifier_main_defaults_to_this_checkout(
    lambda_verifier: Any, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """``main`` with no arguments must resolve the repository it ships in."""
    seen = _patch_main(monkeypatch, lambda_verifier)
    assert lambda_verifier.main([]) == 0
    assert len(seen[0]) == len(EXPECTED_TARGETS)
    assert f"Imported {len(EXPECTED_TARGETS)} Python Lambda handlers." in capsys.readouterr().out


def test_lambda_verifier_main_accepts_an_explicit_root(
    lambda_verifier: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write(tmp_path / "lambda" / "only-handler" / "handler.py", "def lambda_handler(): ...\n")
    runtime = ROOT / lambda_verifier.RUNTIME_PACKAGES
    _write(tmp_path / lambda_verifier.RUNTIME_PACKAGES, runtime.read_text(encoding="utf-8"))
    seen = _patch_main(monkeypatch, lambda_verifier)
    # The real tables name handlers in this repository, which are absent from a
    # synthetic root; discovery would (correctly) reject them as stale.
    for table in ("ENTRYPOINT_OVERRIDES", "EXTRA_SOURCE_MODULES", "BUILD_DIRECTORIES"):
        monkeypatch.setattr(lambda_verifier, table, {})
    monkeypatch.setattr(lambda_verifier, "IMAGE_CONTEXTS", {})
    assert lambda_verifier.main(["--root", str(tmp_path), "--uv", "/opt/uv"]) == 0
    assert [target.name for target in seen[0]] == ["only-handler"]


@pytest.mark.parametrize(
    ("failures", "skipped", "require_all", "code", "message"),
    [
        (["x failed"], [], False, 1, "1 of 19 Python Lambda handlers failed to import."),
        ([], ["y skipped"], False, 0, "Imported 18 Python Lambda handlers; skipped 1 (see above)."),
        ([], ["y skipped"], True, 1, "--require-all: 1 handlers were skipped on this host."),
    ],
)
def test_lambda_verifier_main_reports_failures_and_skips(
    lambda_verifier: Any,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    failures: list[str],
    skipped: list[str],
    require_all: bool,
    code: int,
    message: str,
) -> None:
    _patch_main(monkeypatch, lambda_verifier, failures=failures, skipped=skipped)
    argv = ["--require-all"] if require_all else []
    assert lambda_verifier.main(argv) == code
    assert message in capsys.readouterr().out


def test_lambda_verifier_main_needs_uv(
    lambda_verifier: Any, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _patch_main(monkeypatch, lambda_verifier)
    monkeypatch.setattr(lambda_verifier.shutil, "which", lambda name: None)
    with pytest.raises(SystemExit) as raised:
        lambda_verifier.main([])
    assert raised.value.code == 2
    assert "use-pinned-uv.sh" in capsys.readouterr().err


def test_lambda_verifier_main_needs_the_runtime_interpreter(
    lambda_verifier: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _write(
        tmp_path / lambda_verifier.RUNTIME_PACKAGES,
        "# lambda-runtime: python2.7\nboto3==1.0.0\nbotocore==1.0.0\n",
    )
    _patch_main(monkeypatch, lambda_verifier)
    with pytest.raises(SystemExit) as raised:
        lambda_verifier.main(["--root", str(tmp_path)])
    assert raised.value.code == 2
    assert "run this check under python2.7" in capsys.readouterr().err


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
