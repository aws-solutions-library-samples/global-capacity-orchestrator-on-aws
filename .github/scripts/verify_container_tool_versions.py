#!/usr/bin/env python3
"""Verify that CI-built container tools report their reviewed source pins."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
from collections.abc import Callable, Sequence
from pathlib import Path

CommandRunner = Callable[[list[str]], str]
DEV_PIN_NAMES = (
    "NODE_VERSION",
    "NPM_VERSION",
    "CDK_VERSION",
    "KUBECTL_VERSION",
    "AWSCLI_VERSION",
    "DOCKER_VERSION",
    "BUILDX_VERSION",
    "UV_VERSION",
)


class VerificationError(RuntimeError):
    """A pin is missing, output is malformed, or a runtime version drifted."""


def parse_dev_pins(dockerfile: Path) -> dict[str, str]:
    """Read the exact tool-version ARGs from ``Dockerfile.dev``."""
    content = dockerfile.read_text(encoding="utf-8")
    pins = dict(re.findall(r"^ARG ([A-Z0-9_]+)=([^\s#]+)\s*$", content, re.MULTILINE))
    missing = [name for name in DEV_PIN_NAMES if not pins.get(name)]
    if missing:
        raise VerificationError(f"missing Dockerfile.dev pins: {', '.join(missing)}")
    return {name: pins[name] for name in DEV_PIN_NAMES}


def _single_release_pin(content: str, pattern: str, label: str) -> str:
    matches = set(re.findall(pattern, content))
    if len(matches) != 1:
        values = ", ".join(sorted(matches)) or "none"
        raise VerificationError(f"expected one {label} pin, found: {values}")
    return matches.pop()


def parse_helm_installer_pins(dockerfile: Path) -> dict[str, str]:
    """Read Helm and kubectl versions from their authenticated asset URLs."""
    content = dockerfile.read_text(encoding="utf-8")
    return {
        "HELM_VERSION": _single_release_pin(
            content,
            r"helm-(v\d+\.\d+\.\d+)-linux-amd64\.tar\.gz",
            "Helm",
        ),
        "KUBECTL_VERSION": _single_release_pin(
            content,
            r"release/(v\d+\.\d+\.\d+)/bin/linux/amd64/kubectl",
            "kubectl",
        ),
    }


def run_command(command: list[str]) -> str:
    """Run one bounded command and return its primary output stream."""
    completed = subprocess.run(
        command,
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
    )
    output = completed.stdout.strip() or completed.stderr.strip()
    if not output:
        raise VerificationError(f"command produced no version output: {command!r}")
    return output


def _docker_command(
    image: str,
    command: Sequence[str],
    *,
    entrypoint: str | None = None,
) -> list[str]:
    result = ["docker", "run", "--rm"]
    if entrypoint is not None:
        result.extend(["--entrypoint", entrypoint])
    result.append(image)
    result.extend(command)
    return result


def _extract_version(output: str, pattern: str, tool: str) -> str:
    match = re.search(pattern, output, re.MULTILINE)
    if match is None:
        raise VerificationError(f"could not parse {tool} version from: {output!r}")
    return match.group(1)


def _kubectl_version(output: str) -> str:
    try:
        value = json.loads(output)["clientVersion"]["gitVersion"]
    except (KeyError, TypeError, json.JSONDecodeError) as exc:
        raise VerificationError(f"could not parse kubectl version from: {output!r}") from exc
    if not isinstance(value, str) or not re.fullmatch(r"v\d+\.\d+\.\d+", value):
        raise VerificationError(f"invalid kubectl version: {value!r}")
    return value


def _require_version(tool: str, expected: str, actual: str) -> None:
    if actual != expected:
        raise VerificationError(f"expected {tool} {expected}, got {actual}")
    print(f"{tool}: {actual}")


def verify_dev_image(
    image: str,
    dockerfile: Path,
    *,
    runner: CommandRunner = run_command,
) -> dict[str, str]:
    """Compare every contributor-tool runtime version with ``Dockerfile.dev``."""
    pins = parse_dev_pins(dockerfile)
    checks = (
        ("Node.js", "NODE_VERSION", ("node", "--version"), r"^(v\d+\.\d+\.\d+)\b"),
        ("npm", "NPM_VERSION", ("npm", "--version"), r"^(\d+\.\d+\.\d+)\b"),
        ("CDK", "CDK_VERSION", ("cdk", "--version"), r"^(\d+\.\d+\.\d+)\b"),
        (
            "AWS CLI",
            "AWSCLI_VERSION",
            ("aws", "--version"),
            r"\baws-cli/(\d+\.\d+\.\d+)\b",
        ),
        (
            "Docker CLI",
            "DOCKER_VERSION",
            ("docker", "--version"),
            r"\bDocker version (\d+\.\d+\.\d+),",
        ),
        (
            "Buildx",
            "BUILDX_VERSION",
            ("docker", "buildx", "version"),
            r"\b(v\d+\.\d+\.\d+)\b",
        ),
        ("uv", "UV_VERSION", ("uv", "--version"), r"^uv (\d+\.\d+\.\d+)\b"),
        ("uvx", "UV_VERSION", ("uvx", "--version"), r"^uvx (\d+\.\d+\.\d+)\b"),
    )
    actual_versions: dict[str, str] = {}
    for tool, pin_name, command, pattern in checks:
        output = runner(_docker_command(image, command))
        actual = _extract_version(output, pattern, tool)
        _require_version(tool, pins[pin_name], actual)
        actual_versions[tool] = actual

    kubectl_output = runner(
        _docker_command(
            image,
            ("kubectl", "version", "--client=true", "--output=json"),
        )
    )
    kubectl_actual = _kubectl_version(kubectl_output)
    _require_version("kubectl", pins["KUBECTL_VERSION"], kubectl_actual)
    actual_versions["kubectl"] = kubectl_actual
    return actual_versions


def verify_helm_installer_image(
    image: str,
    dockerfile: Path,
    *,
    runner: CommandRunner = run_command,
) -> dict[str, str]:
    """Compare Helm-installer runtime binaries with authenticated URL pins."""
    pins = parse_helm_installer_pins(dockerfile)
    helm_output = runner(_docker_command(image, ("version", "--short"), entrypoint="helm"))
    helm_actual = _extract_version(helm_output, r"\b(v\d+\.\d+\.\d+)\b", "Helm")
    _require_version("Helm", pins["HELM_VERSION"], helm_actual)

    kubectl_output = runner(
        _docker_command(
            image,
            ("version", "--client=true", "--output=json"),
            entrypoint="kubectl",
        )
    )
    kubectl_actual = _kubectl_version(kubectl_output)
    _require_version("kubectl", pins["KUBECTL_VERSION"], kubectl_actual)
    return {"Helm": helm_actual, "kubectl": kubectl_actual}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("profile", choices=("dev", "helm-installer"))
    parser.add_argument("--image", required=True)
    parser.add_argument("--dockerfile", type=Path)
    args = parser.parse_args(argv)

    root = Path(__file__).resolve().parents[2]
    if args.profile == "dev":
        dockerfile = args.dockerfile or root / "Dockerfile.dev"
        verify_dev_image(args.image, dockerfile)
    else:
        dockerfile = args.dockerfile or root / "lambda" / "helm-installer" / "Dockerfile"
        verify_helm_installer_image(args.image, dockerfile)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
