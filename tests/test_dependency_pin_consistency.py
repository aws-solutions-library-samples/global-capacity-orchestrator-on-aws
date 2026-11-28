"""Guard duplicate dependency declarations against version-pin drift."""

from __future__ import annotations

import re
import tomllib
from collections import defaultdict
from pathlib import Path

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

PROJECT_ROOT = Path(__file__).resolve().parents[1]

#: The Lambda builds that install a requirements.txt, each constrained by
#: requirements-lock.txt: the kubectl-applier-simple package the CLI builds
#: (``StackManager._build_kubectl_lambda``) and the two container images.
LOCKED_LAMBDA_REQUIREMENTS = (
    "lambda/helm-installer/requirements.txt",
    "lambda/kubectl-applier-simple/requirements.txt",
    "lambda/tls-certificate-manager/requirements.txt",
)


def _locked_versions() -> dict[str, str]:
    """Package name -> version for every ``name==version`` line of the lock."""
    text = (PROJECT_ROOT / "requirements-lock.txt").read_text(encoding="utf-8")
    return {
        canonicalize_name(name): version
        for name, version in re.findall(r"^([A-Za-z0-9][A-Za-z0-9._-]*)==(\S+)", text, re.M)
    }


def test_duplicate_dependency_pins_are_consistent() -> None:
    """A package repeated anywhere in pyproject.toml must use one specifier."""
    with (PROJECT_ROOT / "pyproject.toml").open("rb") as pyproject_file:
        pyproject = tomllib.load(pyproject_file)

    declarations: list[tuple[str, list[str]]] = [
        ("project.dependencies", pyproject["project"].get("dependencies", [])),
    ]
    declarations.extend(
        (f"project.optional-dependencies.{group}", requirements)
        for group, requirements in pyproject["project"].get("optional-dependencies", {}).items()
    )

    locations_by_package: dict[str, dict[str, list[str]]] = defaultdict(lambda: defaultdict(list))
    for section, requirements in declarations:
        for raw_requirement in requirements:
            requirement = Requirement(raw_requirement)
            package = canonicalize_name(requirement.name)
            specifier = str(requirement.specifier)
            locations_by_package[package][specifier].append(f"{section}: {raw_requirement}")

    conflicts = []
    for package, locations_by_specifier in sorted(locations_by_package.items()):
        if len(locations_by_specifier) < 2:
            continue
        details = "; ".join(
            f"{specifier or '<unpinned>'} at {', '.join(locations)}"
            for specifier, locations in sorted(locations_by_specifier.items())
        )
        conflicts.append(f"{package}: {details}")

    assert not conflicts, "Inconsistent dependency pins:\n" + "\n".join(conflicts)


def test_lambda_build_requirements_are_pinned_at_their_locked_versions() -> None:
    """Every package a Lambda build installs directly is in the lock at that version.

    The builds install requirements.txt with requirements-lock.txt as a
    constraints file. A pin the lock disagrees with makes pip refuse to
    resolve, and a package the lock does not list would leave that package's
    own dependencies outside the lock, so both fail here first. The tuple is
    held to the builds: the kubectl-applier package plus every Lambda image.
    """
    images = {
        f"lambda/{path.parent.name}/requirements.txt"
        for path in (PROJECT_ROOT / "lambda").glob("*/Dockerfile")
        if not path.parent.name.endswith("-build")
    }
    assert set(LOCKED_LAMBDA_REQUIREMENTS) == images | {
        "lambda/kubectl-applier-simple/requirements.txt"
    }

    locked = _locked_versions()
    assert locked["boto3"]  # the parser reads the lock's pins
    problems = []
    for relative_path in LOCKED_LAMBDA_REQUIREMENTS:
        for raw_line in (PROJECT_ROOT / relative_path).read_text(encoding="utf-8").splitlines():
            line = raw_line.split("#", 1)[0].strip()
            if not line:
                continue
            requirement = Requirement(line)
            name = canonicalize_name(requirement.name)
            pins = [spec.version for spec in requirement.specifier if spec.operator == "=="]
            if len(requirement.specifier) != 1 or len(pins) != 1:
                problems.append(f"{relative_path}: {line} is not one exact == pin")
            elif locked.get(name) != pins[0]:
                problems.append(
                    f"{relative_path}: {line}, but requirements-lock.txt has "
                    f"{name}=={locked.get(name, '<missing>')}"
                )

    assert not problems, "Lambda pins disagree with the lock:\n" + "\n".join(problems)
