"""Guard duplicate dependency declarations against version-pin drift."""

from __future__ import annotations

import tomllib
from collections import defaultdict
from pathlib import Path

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

PROJECT_ROOT = Path(__file__).resolve().parents[1]


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
