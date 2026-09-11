"""Behavioral tests for CI import and container-version verification helpers."""

from __future__ import annotations

import importlib.util
import json
import re
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / ".github" / "scripts"
EXPECTED_HANDLER_ENTRYPOINTS = {
    "analytics-cleanup": "handler",
    "analytics-presigned-url": "lambda_handler",
    "api-gateway-proxy": "lambda_handler",
    "capacity-poller": "lambda_handler",
    "cross-region-aggregator": "lambda_handler",
    "drift-detection": "lambda_handler",
    "ga-registration": "lambda_handler",
    "helm-installer": "lambda_handler",
    "helm-orchestrator": "on_event",
    "image-lookup": "lambda_handler",
    "kubectl-applier-simple": "lambda_handler",
    "regional-api-proxy": "lambda_handler",
    "secret-rotation": "lambda_handler",
    "tls-certificate-manager": "lambda_handler",
    "traffic-dial-controller": "lambda_handler",
    "vector-ingest": "lambda_handler",
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


def test_lambda_verifier_discovers_and_imports_every_handler(lambda_verifier: Any) -> None:
    targets = lambda_verifier.discover_handlers(ROOT)
    actual = {target.directory.name: target.entrypoint for target in targets}

    assert actual == EXPECTED_HANDLER_ENTRYPOINTS
    lambda_verifier.verify_handlers(targets)


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


def test_lambda_verifier_refuses_an_override_naming_a_missing_handler(
    lambda_verifier: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stale entrypoint override is a silent hole, so it fails loudly.

    Overrides exist for the handlers whose entrypoint is not the default. If one
    is renamed or deleted and its override is left behind, the override stops
    applying to anything -- and the handler it was meant to describe would be
    verified against the wrong entrypoint name, or not at all.
    """
    handler_dir = tmp_path / "lambda" / "real-handler"
    handler_dir.mkdir(parents=True)
    (handler_dir / "handler.py").write_text("def lambda_handler():\n    pass\n", encoding="utf-8")
    monkeypatch.setitem(lambda_verifier.ENTRYPOINT_OVERRIDES, "handler-that-moved", "main")

    with pytest.raises(RuntimeError, match="overrides reference missing handlers"):
        lambda_verifier.discover_handlers(tmp_path)


def test_lambda_verifier_main_defaults_to_this_checkout(
    lambda_verifier: Any, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """``main`` with no arguments must resolve the repository it ships in."""
    verified: list[object] = []
    monkeypatch.setattr(
        lambda_verifier, "verify_handler", lambda target, **_: verified.append(target)
    )

    assert lambda_verifier.main([]) == 0

    names = {target.directory.name for target in verified}
    assert names == set(EXPECTED_HANDLER_ENTRYPOINTS), (
        "main() did not discover the same handlers as an explicit --root"
    )
    assert f"Imported {len(verified)} Python Lambda handlers." in capsys.readouterr().out


def test_lambda_verifier_main_accepts_an_explicit_root(
    lambda_verifier: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    handler_dir = tmp_path / "lambda" / "only-handler"
    handler_dir.mkdir(parents=True)
    (handler_dir / "handler.py").write_text("def lambda_handler():\n    pass\n", encoding="utf-8")
    verified: list[object] = []
    monkeypatch.setattr(
        lambda_verifier, "verify_handler", lambda target, **_: verified.append(target)
    )
    # The real overrides name handlers in this repository, which are absent from
    # a synthetic root -- discovery would (correctly) reject them as stale.
    monkeypatch.setattr(lambda_verifier, "ENTRYPOINT_OVERRIDES", {})

    assert lambda_verifier.main(["--root", str(tmp_path)]) == 0
    assert [target.directory.name for target in verified] == ["only-handler"]


def _dev_outputs() -> dict[tuple[str, ...], str]:
    return {
        ("node", "--version"): "v24.21.0",
        ("npm", "--version"): "12.0.2",
        ("cdk", "--version"): "2.1140.0 (build abc123)",
        ("aws", "--version"): "aws-cli/2.36.41 Python/3.13.11 Linux/6.11",
        ("docker", "--version"): "Docker version 29.8.0, build deadbeef",
        ("docker", "buildx", "version"): "github.com/docker/buildx v0.37.0 abc123",
        ("uv", "--version"): "uv 0.12.11 (abc123 2026-08-01)",
        ("uvx", "--version"): "uvx 0.12.11 (abc123 2026-08-01)",
        (
            "kubectl",
            "version",
            "--client=true",
            "--output=json",
        ): json.dumps({"clientVersion": {"gitVersion": "v1.36.4"}}),
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
        "Node.js": "v24.21.0",
        "npm": "12.0.2",
        "CDK": "2.1140.0",
        "AWS CLI": "2.36.41",
        "Docker CLI": "29.8.0",
        "Buildx": "v0.37.0",
        "uv": "0.12.11",
        "uvx": "0.12.11",
        "kubectl": "v1.36.4",
    }


def test_dev_verifier_rejects_a_valid_but_wrong_runtime_version(
    container_verifier: Any,
) -> None:
    outputs = _dev_outputs()
    outputs[("node", "--version")] = "v24.18.0"

    with pytest.raises(container_verifier.VerificationError, match="expected Node.js"):
        container_verifier.verify_dev_image(
            "gco-dev",
            ROOT / "Dockerfile.dev",
            runner=_dev_runner(outputs),
        )


def _helm_runner(*, kubectl_version: str = "v1.36.4"):
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
        ): "v4.2.4+gabcdef",
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

    assert actual == {"Helm": "v4.2.4", "kubectl": "v1.36.4"}


def test_helm_installer_verifier_rejects_a_mismatched_runtime_version(
    container_verifier: Any,
) -> None:
    with pytest.raises(container_verifier.VerificationError, match="expected kubectl"):
        container_verifier.verify_helm_installer_image(
            "helm-installer:ci",
            ROOT / "lambda" / "helm-installer" / "Dockerfile",
            runner=_helm_runner(kubectl_version="v1.36.2"),
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

    with pytest.raises(container_verifier.VerificationError, match="missing Dockerfile.dev pins"):
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
    with pytest.raises(container_verifier.VerificationError, match="could not parse Node.js"):
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


@pytest.mark.parametrize("profile", ("dev", "helm-installer"))
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
