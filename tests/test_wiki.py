"""Guard tests for the orientation wiki (wiki/ + mkdocs.yml).

The wiki is a routing layer over the repository's documentation, so its most
likely rot is referential: a renamed file breaking a GitHub deep link, a page
falling out of the MkDocs nav, or a screenshot rename orphaning an image
reference. These tests make each of those a PR-time failure, mirroring the
symmetry style of ``tests/test_mcp_docs_index.py``.

Deliberately pure-stdlib plus ``yaml.safe_load`` — no MkDocs import — which
is why ``mkdocs.yml`` must never grow custom YAML tags (``!!python/name:``).
The ``assets/images/`` → ``images/`` mapping asserted here mirrors the
injection hook in ``scripts/mkdocs_hooks.py``.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent
WIKI_DIR = PROJECT_ROOT / "wiki"
MKDOCS_YML = PROJECT_ROOT / "mkdocs.yml"

#: Canonical Pages origin; the nav's external coverage entry must live there.
PAGES_ORIGIN = "https://awslabs.github.io/global-capacity-orchestrator-on-aws/"

#: GitHub deep links into this repository, as required by the wiki content
#: contract (docs/ is not part of the built site, so wiki pages link to
#: GitHub). The captured group is the in-repo path.
_REPO_LINK = re.compile(
    r"https://github\.com/awslabs/global-capacity-orchestrator-on-aws/"
    r"(?:blob|tree)/main/([^)\"'#\s]+)"
)

#: Markdown inline links/images plus HTML src/href attributes.
_MD_TARGET = re.compile(r"!?\[[^\]]*\]\(([^)\s]+)")
_HTML_TARGET = re.compile(r"(?:src|href)=\"([^\"]+)\"")

#: The image-injection mapping from scripts/mkdocs_hooks.py.
_ASSETS_PREFIX = "assets/images/"
_IMAGES_DIR = PROJECT_ROOT / "images"


def _nav_entries(node: object) -> list[str]:
    """Flatten the mkdocs nav tree into its string leaves (pages + URLs)."""
    leaves: list[str] = []
    if isinstance(node, str):
        leaves.append(node)
    elif isinstance(node, list):
        for item in node:
            leaves.extend(_nav_entries(item))
    elif isinstance(node, dict):
        for value in node.values():
            leaves.extend(_nav_entries(value))
    return leaves


def _load_nav() -> list[str]:
    config = yaml.safe_load(MKDOCS_YML.read_text(encoding="utf-8"))
    return _nav_entries(config["nav"])


def _wiki_pages() -> dict[str, str]:
    return {path.name: path.read_text(encoding="utf-8") for path in sorted(WIKI_DIR.glob("*.md"))}


def _link_targets(text: str) -> set[str]:
    return set(_MD_TARGET.findall(text)) | set(_HTML_TARGET.findall(text))


# =============================================================================
# Nav ↔ wiki/*.md symmetry
# =============================================================================


def test_mkdocs_yml_is_safe_loadable_with_a_nav() -> None:
    """The guard contract itself: plain YAML, nav present, docs_dir is wiki."""
    config = yaml.safe_load(MKDOCS_YML.read_text(encoding="utf-8"))
    assert config["docs_dir"] == "wiki"
    assert config["strict"] is True
    assert isinstance(config["nav"], list) and config["nav"]


def test_nav_and_wiki_pages_are_one_to_one() -> None:
    """Every wiki page is reachable from the nav, and the nav lists no ghosts."""
    nav_pages = {entry for entry in _load_nav() if entry.endswith(".md")}
    wiki_pages = set(_wiki_pages())
    assert nav_pages == wiki_pages, (
        f"nav/wiki mismatch — pages missing from nav: {sorted(wiki_pages - nav_pages)}, "
        f"nav entries with no file: {sorted(nav_pages - wiki_pages)}"
    )


def test_nav_coverage_entry_is_the_canonical_pages_url() -> None:
    """The coverage report is merged by pages.yml at /coverage/ — the nav's
    external entry must keep pointing exactly there, on the canonical origin,
    or the report silently falls out of the site's navigation."""
    external = [entry for entry in _load_nav() if entry.startswith("http")]
    assert external == [f"{PAGES_ORIGIN}coverage/"], (
        f"expected exactly one external nav entry at {PAGES_ORIGIN}coverage/, got {external}"
    )


# =============================================================================
# Repo-facing link integrity
# =============================================================================


def test_every_github_deep_link_resolves_to_a_repo_path() -> None:
    """blob/tree deep links must point at files/dirs that exist in this checkout."""
    missing: list[str] = []
    for name, text in _wiki_pages().items():
        for repo_path in _REPO_LINK.findall(text):
            if not (PROJECT_ROOT / repo_path).exists():
                missing.append(f"{name} -> {repo_path}")
    assert not missing, f"wiki links to nonexistent repository paths: {missing}"


def test_relative_links_resolve_to_wiki_pages_or_injected_assets() -> None:
    """Non-URL targets must be sibling wiki pages or hook-injected images."""
    problems: list[str] = []
    for name, text in _wiki_pages().items():
        for target in _link_targets(text):
            if target.startswith(("http://", "https://", "mailto:")):
                continue
            path = target.split("#", 1)[0]
            if not path:
                continue  # pure-fragment link within the page
            if path.startswith(_ASSETS_PREFIX):
                if not (_IMAGES_DIR / path.removeprefix(_ASSETS_PREFIX)).is_file():
                    problems.append(f"{name} -> {target} (no matching images/ file)")
            elif not (WIKI_DIR / path).is_file():
                problems.append(f"{name} -> {target} (not a wiki page)")
    assert not problems, f"unresolvable relative links: {problems}"


def test_wiki_uses_no_external_image_hosts() -> None:
    """Requirement 4.2: images come from the site itself, never external hosts."""
    offenders: list[str] = []
    for name, text in _wiki_pages().items():
        for match in re.findall(r"!\[[^\]]*\]\(([^)\s]+)", text):
            if match.startswith(("http://", "https://")):
                offenders.append(f"{name} -> {match}")
    assert not offenders, f"externally hosted images in wiki pages: {offenders}"


# =============================================================================
# Image-reference integrity (hook mapping)
# =============================================================================


def test_every_wiki_image_maps_to_a_tracked_asset() -> None:
    """assets/images/<name> must exist as images/<name> (the hook's mapping)."""
    missing: list[str] = []
    for name, text in _wiki_pages().items():
        for target in _link_targets(text):
            if target.startswith(_ASSETS_PREFIX):
                asset = _IMAGES_DIR / target.removeprefix(_ASSETS_PREFIX)
                if not asset.is_file():
                    missing.append(f"{name} -> {target}")
    assert not missing, f"wiki references images that do not exist under images/: {missing}"


def test_wiki_pages_carry_no_reference_to_docs_dir_pages_as_relative_links() -> None:
    """docs/ is not part of the built site; a relative docs/ link would 404.

    The content contract (requirements 2.4) says deep documentation is linked
    via full GitHub URLs — this catches the natural authoring mistake.
    """
    offenders: list[str] = []
    for name, text in _wiki_pages().items():
        for target in _link_targets(text):
            if target.startswith(("docs/", "../docs/")):
                offenders.append(f"{name} -> {target}")
    assert not offenders, (
        f"relative docs/ links would 404 on the built site (use GitHub blob URLs): {offenders}"
    )
