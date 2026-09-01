"""Bidirectional contracts for human-facing repository inventories."""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

from cli.main import cli

ROOT = Path(__file__).resolve().parents[1]

_PRIMARY_WORKFLOWS = {
    "unit-tests.yml",
    "inference-streaming-proxy.yml",
    "floci-tests.yml",
    "integration-tests.yml",
    "security.yml",
    "lint.yml",
}


def _section(text: str, start: str, end: str | None) -> str:
    assert start in text, f"missing documentation section {start!r}"
    section = text.split(start, 1)[1]
    if end is not None:
        assert end in section, f"missing documentation section boundary {end!r}"
        section = section.split(end, 1)[0]
    return section


def _workflow_rows(section: str) -> set[str]:
    workflows: set[str] = set()
    for line in section.splitlines():
        if not line.startswith("|"):
            continue
        first_cell = line.split("|", 2)[1]
        match = re.search(
            r"(?:\.github/)?(?:workflows/)?([a-z0-9-]+\.yml)\b",
            first_cell,
        )
        if match:
            workflows.add(match.group(1))
    return workflows


def test_top_level_docs_index_is_exact() -> None:
    docs_dir = ROOT / "docs"
    actual = {path.name for path in docs_dir.glob("*.md") if path.name != "README.md"}
    index = (docs_dir / "README.md").read_text(encoding="utf-8")
    indexed = {
        target for target in re.findall(r"\]\(([A-Z][A-Z0-9_]+\.md)\)", index) if "/" not in target
    }
    assert len(actual) == 31
    assert indexed == actual, (
        f"docs/README.md drifted; missing={sorted(actual - indexed)!r}, "
        f"stale={sorted(indexed - actual)!r}"
    )


def test_cli_toc_and_module_readme_match_command_modules() -> None:
    modules = sorted((ROOT / "cli" / "commands").glob("*_cmd.py"))
    expected_groups = set(cli.commands)
    expected_files = {path.name for path in modules}
    assert len(expected_groups) == len(expected_files) == 27

    cli_doc = (ROOT / "docs" / "CLI.md").read_text(encoding="utf-8")
    toc = _section(cli_doc, "## Table of Contents", "## Installation")
    documented_groups = set(re.findall(r"^  - \[([^]]+)\]\(#[^)]+-commands?\)$", toc, re.MULTILINE))
    assert documented_groups == expected_groups, (
        f"docs/CLI.md command TOC drifted; "
        f"missing={sorted(expected_groups - documented_groups)!r}, "
        f"stale={sorted(documented_groups - expected_groups)!r}"
    )

    module_readme = (ROOT / "cli" / "README.md").read_text(encoding="utf-8")
    command_table = _section(module_readme, "### commands/", "### capacity/")
    documented_files = set(
        re.findall(r"^\| `([a-z0-9_]+_cmd\.py)` \|", command_table, re.MULTILINE)
    )
    assert documented_files == expected_files, (
        f"cli/README.md command table drifted; "
        f"missing={sorted(expected_files - documented_files)!r}, "
        f"stale={sorted(documented_files - expected_files)!r}"
    )


def test_workflow_inventories_are_complete_and_partitioned() -> None:
    actual = {path.name for path in (ROOT / ".github" / "workflows").glob("*.yml")}
    satellites = actual - _PRIMARY_WORKFLOWS
    assert len(actual) == 14
    assert len(_PRIMARY_WORKFLOWS) == 6
    assert len(satellites) == 8

    inventories = (
        (
            ROOT / ".github" / "CI.md",
            "### Primary (run on every push + PR)",
            "### Satellites",
            "### Naming conventions",
        ),
        (
            ROOT / ".github" / "workflows" / "README.md",
            "## Primary Workflows",
            "## Satellite Workflows",
            "## Naming Conventions",
        ),
        (
            ROOT / "CONTRIBUTING.md",
            "#### Primary workflows (run on every push + PR)",
            "#### Satellite workflows",
            "#### Published coverage report and badge",
        ),
    )
    for path, primary_heading, satellite_heading, end_heading in inventories:
        text = path.read_text(encoding="utf-8")
        primary = _workflow_rows(_section(text, primary_heading, satellite_heading))
        satellite = _workflow_rows(_section(text, satellite_heading, end_heading))
        assert primary == _PRIMARY_WORKFLOWS, (
            f"{path.relative_to(ROOT)} primary workflow drift: "
            f"missing={sorted(_PRIMARY_WORKFLOWS - primary)!r}, "
            f"extra={sorted(primary - _PRIMARY_WORKFLOWS)!r}"
        )
        assert satellite == satellites, (
            f"{path.relative_to(ROOT)} satellite workflow drift: "
            f"missing={sorted(satellites - satellite)!r}, "
            f"extra={sorted(satellite - satellites)!r}"
        )
        assert not primary & satellite
        assert primary | satellite == actual

    wiki = (ROOT / "wiki" / "build-and-test.md").read_text(encoding="utf-8")
    assert "Six primary workflows" in wiki
    assert "Eight satellite workflows" in wiki


def test_image_dependency_groups_match_dockerfiles_one_to_one() -> None:
    config = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    groups = {
        name for name in config["project"]["optional-dependencies"] if name.startswith("image-")
    }
    dockerfiles = sorted((ROOT / "dockerfiles").glob("*-dockerfile"))
    dockerfile_groups = {f"image-{path.name.removesuffix('-dockerfile')}" for path in dockerfiles}
    assert len(groups) == len(dockerfile_groups) == 6
    assert groups == dockerfile_groups

    selector = re.compile(r'optional-dependencies"\]\["(image-[a-z0-9-]+)"\]')
    for path in dockerfiles:
        expected = f"image-{path.name.removesuffix('-dockerfile')}"
        selected = selector.findall(path.read_text(encoding="utf-8"))
        assert selected == [expected], (
            f"{path.relative_to(ROOT)} must select exactly {expected}, got {selected}"
        )

    contributing = (ROOT / "CONTRIBUTING.md").read_text(encoding="utf-8")
    assert "The six `image-*` groups" in contributing
    assert all(f"[{group}]" in contributing for group in groups)
