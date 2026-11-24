"""Checks that keep ``docs/ANALYTICS.md`` in step with what the code does.

Each test pins a way the guide went wrong before:

* Its cross-references named the wrong section: ``section (l)`` for the EFS
  retain override, which is section (k); ``section (n)``, which does not
  exist. Every reference is now a link whose anchor is that section's
  heading, so the label and the target cannot disagree and a reader can
  see which section is meant.
* The Studio sections told users to ``pip install -e`` a package that needs
  Python 3.14 on an image that runs 3.13, to run ``python -m mcp.run_mcp``
  (which imports the MCP SDK's ``mcp`` package, not GCO's server), and to
  run commands the Studio execution role cannot (``jobs submit-direct``
  needs kubectl, ``inference deploy`` writes DynamoDB, ``capacity`` reads
  EC2, SQS and Pricing).
* The ``GCO_REF`` the Studio install pins has to follow ``VERSION`` the way
  the MCP guide's does, or the guide installs an old release.
"""

from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
GUIDE = ROOT / "docs" / "ANALYTICS.md"


def _guide() -> str:
    return GUIDE.read_text(encoding="utf-8")


def _section_slugs(text: str) -> dict[str, str]:
    """``{"k": "k-efs-persistent-home-folder-behavior", ...}`` for each ``## (x)`` heading.

    The slug is GitHub's: lower case, punctuation other than ``-`` and ``_``
    dropped, spaces turned into hyphens.
    """
    slugs: dict[str, str] = {}
    for match in re.finditer(r"(?m)^## \(([a-z][0-9]?)\) (.+)$", text):
        heading = f"({match.group(1)}) {match.group(2)}".lower()
        slug = re.sub(r"[^\w\- ]", "", heading).replace(" ", "-")
        slugs[match.group(1)] = slug
    return slugs


def _fenced_blocks(text: str, language: str) -> list[str]:
    return re.findall(rf"(?ms)^```{language}\n(.*?)^```", text)


def _studio_sections(text: str) -> str:
    """Sections (e) through (i): what a user runs inside Studio."""
    start = text.index("\n## (e) ")
    end = text.index("\n## (j) ")
    return text[start:end]


def test_every_section_reference_links_to_that_sections_heading() -> None:
    text = _guide()
    slugs = _section_slugs(text)
    assert {"a", "e", "k", "m"} <= set(slugs), "the lettered sections were renamed"

    references = re.findall(r"\[sections? \(([a-z][0-9]?)\)\]\(#([^)]+)\)", text)
    assert references, "the guide no longer cross-references its sections"
    for label, anchor in references:
        assert label in slugs, f"section ({label}) does not exist"
        assert anchor == slugs[label], (
            f"[section ({label})] links to #{anchor}, but section ({label}) is #{slugs[label]}"
        )


def test_no_section_is_referenced_without_a_link() -> None:
    """A plain ``section (x)`` can name the wrong section and nothing notices."""
    text = _guide()
    unlinked = [
        match.group(0)
        for match in re.finditer(r"(?<!\[)\bsections? \([a-z][0-9]?\)", text, re.IGNORECASE)
    ]
    assert unlinked == []


@pytest.mark.parametrize(
    "command",
    [
        "pip install -e",
        "python -m mcp.run_mcp",
        "gco jobs submit-direct",
        "gco jobs submit-sqs",
        "gco inference deploy",
        "gco capacity",
    ],
)
def test_studio_sections_run_only_what_the_studio_role_can(command: str) -> None:
    """Only the code blocks are checked: the prose names these as workstation-only."""
    blocks = "\n".join(
        _fenced_blocks(_studio_sections(_guide()), "bash")
        + _fenced_blocks(_studio_sections(_guide()), "python")
    )
    assert command not in blocks


def test_studio_install_pins_python_314_and_the_release_ref() -> None:
    text = _guide()
    version = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
    requires = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"][
        "requires-python"
    ]
    assert requires == ">=3.14"

    assert re.findall(r"(?m)^GCO_REF=(v\d+\.\d+\.\d+)\b", text) == [f"v{version}"]
    installs = re.findall(r"(?m)^uv tool install .*$", text)
    # The repository URL is whatever the guide names (a fork rewrites it);
    # the pin and the interpreter are what must hold.
    assert len(installs) == 1
    assert re.fullmatch(
        r'uv tool install --python 3\.14 "git\+https://github\.com/[^/"]+/[^/"]+\.git@\$\{GCO_REF\}"',
        installs[0],
    ), installs[0]


def test_mcp_server_examples_run_a_console_script_gco_ships() -> None:
    scripts = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"][
        "scripts"
    ]
    servers = [
        server
        for block in _fenced_blocks(_guide(), "json")
        if "mcpServers" in block
        for server in json.loads(block)["mcpServers"].values()
    ]
    assert servers, "the guide no longer shows an MCP server configuration"
    for server in servers:
        assert Path(server["command"]).name in scripts, server
        assert "args" not in server
        assert "cwd" not in server
