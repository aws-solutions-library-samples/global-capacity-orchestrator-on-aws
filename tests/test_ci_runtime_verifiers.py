"""Behavioral tests for CI import and container-version verification helpers."""

from __future__ import annotations

import importlib.util
import json
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


def _dev_outputs() -> dict[tuple[str, ...], str]:
    return {
        ("node", "--version"): "v24.19.0",
        ("npm", "--version"): "12.0.2",
        ("cdk", "--version"): "2.1135.1 (build abc123)",
        ("aws", "--version"): "aws-cli/2.36.19 Python/3.13.11 Linux/6.11",
        ("docker", "--version"): "Docker version 29.7.2, build deadbeef",
        ("docker", "buildx", "version"): "github.com/docker/buildx v0.36.1 abc123",
        ("uv", "--version"): "uv 0.12.3 (abc123 2026-08-01)",
        ("uvx", "--version"): "uvx 0.12.3 (abc123 2026-08-01)",
        (
            "kubectl",
            "version",
            "--client=true",
            "--output=json",
        ): json.dumps({"clientVersion": {"gitVersion": "v1.36.3"}}),
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
        "Node.js": "v24.19.0",
        "npm": "12.0.2",
        "CDK": "2.1135.1",
        "AWS CLI": "2.36.19",
        "Docker CLI": "29.7.2",
        "Buildx": "v0.36.1",
        "uv": "0.12.3",
        "uvx": "0.12.3",
        "kubectl": "v1.36.3",
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


def _helm_runner(*, kubectl_version: str = "v1.36.3"):
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
        ): "v4.2.3+gabcdef",
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

    assert actual == {"Helm": "v4.2.3", "kubectl": "v1.36.3"}


def test_helm_installer_verifier_rejects_a_mismatched_runtime_version(
    container_verifier: Any,
) -> None:
    with pytest.raises(container_verifier.VerificationError, match="expected kubectl"):
        container_verifier.verify_helm_installer_image(
            "helm-installer:ci",
            ROOT / "lambda" / "helm-installer" / "Dockerfile",
            runner=_helm_runner(kubectl_version="v1.36.2"),
        )
