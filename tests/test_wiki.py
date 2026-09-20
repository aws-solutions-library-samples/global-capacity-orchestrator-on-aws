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

``wiki/README.md`` is the one file in the directory that is *not* a page: it
documents the directory for contributors browsing GitHub and is excluded from
the build by ``exclude_docs`` (MkDocs would otherwise refuse the
README.md/index.md conflict under ``strict``). The page-level guards skip it;
the README-level guards at the end pin that exclusion, keep it out of the
nav, and require it to describe every page and every supporting file. Its
links resolve against the source tree, so ``tests/test_markdown_links.py``
covers them.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent
WIKI_DIR = PROJECT_ROOT / "wiki"
MKDOCS_YML = PROJECT_ROOT / "mkdocs.yml"

#: Canonical Pages origin; the nav's external coverage entry must live there.
PAGES_ORIGIN = (
    "https://aws-solutions-library-samples.github.io/global-capacity-orchestrator-on-aws/"
)

#: Canonical repository URL. Kept as a plain literal (and the deep-link regex
#: built from it with ``re.escape``) so scripts/migrate_fork.py's existing
#: repo-url rule rewrites it on forks — the guard then keeps checking the
#: fork's own rewritten wiki links instead of going stale.
REPO_URL = "https://github.com/aws-solutions-library-samples/global-capacity-orchestrator-on-aws"

#: GitHub deep links into this repository, as required by the wiki content
#: contract (docs/ is not part of the built site, so wiki pages link to
#: GitHub). The captured group is the in-repo path.
_REPO_LINK = re.compile(re.escape(REPO_URL) + r"/(?:blob|tree)/main/([^)\"'#\s]+)")

#: Markdown inline links/images plus HTML src/href attributes.
_MD_TARGET = re.compile(r"!?\[[^\]]*\]\(([^)\s]+)")
_HTML_TARGET = re.compile(r"(?:src|href)=\"([^\"]+)\"")

#: The image-injection mapping from scripts/mkdocs_hooks.py.
_ASSETS_PREFIX = "assets/images/"
_IMAGES_DIR = PROJECT_ROOT / "images"

#: The API-spec-sheet injection mapping from scripts/mkdocs_hooks.py: the
#: Markdown files diagrams/api_specs/generate.py renders become api/<name>.md.
_API_PREFIX = "api/"
_API_SPECS_DIR = PROJECT_ROOT / "diagrams" / "api_specs"

#: Where pages.yml builds FastAPI's Swagger UI consoles into the site
#: (diagrams/api_specs/generate.py --swagger-ui-dir); the nav's one non-coverage
#: external entry.
SWAGGER_SITE_PATH = "swagger/"


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


#: The directory's GitHub-facing README: documentation about the wiki, not a page of it.
WIKI_README = WIKI_DIR / "README.md"


def _wiki_pages() -> dict[str, str]:
    """The published pages: every ``wiki/*.md`` except the directory README."""
    return {
        path.name: path.read_text(encoding="utf-8")
        for path in sorted(WIKI_DIR.glob("*.md"))
        if path != WIKI_README
    }


def _injected_api_pages() -> set[str]:
    """The nav entries the hook's api/ injection makes valid."""
    return {f"{_API_PREFIX}{path.name}" for path in _API_SPECS_DIR.glob("*.md")}


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
    """Every wiki page is reachable from the nav, and the nav lists no ghosts.

    The api/ pages are not files under wiki/: they are the generated spec
    sheets the hook injects, so the nav must list exactly those too — a sheet
    left out of the nav is unreachable, and a nav entry without a sheet fails
    the strict build.
    """
    nav_pages = {entry for entry in _load_nav() if entry.endswith(".md")}
    expected = set(_wiki_pages()) | _injected_api_pages()
    assert _injected_api_pages(), "diagrams/api_specs/ ships generated sheets"
    assert nav_pages == expected, (
        f"nav/wiki mismatch — pages missing from nav: {sorted(expected - nav_pages)}, "
        f"nav entries with no file: {sorted(nav_pages - expected)}"
    )


#: Where pages.yml merges each stack's coverage report into the site, in nav
#: order. With the Swagger consoles these are the nav's only external entries.
COVERAGE_REPORT_PATHS = ("python-coverage/", "bash-coverage/", "nodejs-coverage/")


def test_nav_external_entries_are_the_canonical_pages_urls() -> None:
    """Everything pages.yml adds outside the MkDocs build — the Swagger UI
    consoles at /swagger/ and the coverage reports at /python-coverage/,
    /bash-coverage/ and /nodejs-coverage/ — is reached through external nav
    entries, which must keep pointing exactly there, on the canonical origin,
    or a tree silently falls out of the site's navigation."""
    external = [entry for entry in _load_nav() if entry.startswith("http")]
    expected = [f"{PAGES_ORIGIN}{path}" for path in (SWAGGER_SITE_PATH, *COVERAGE_REPORT_PATHS)]
    assert external == expected, (
        f"expected exactly the external nav entries {expected}, got {external}"
    )


def test_pages_workflow_serves_every_tree_the_nav_links() -> None:
    """The nav promises four addresses outside the build; pages.yml must place a tree at each."""
    pages = (PROJECT_ROOT / ".github" / "workflows" / "pages.yml").read_text(encoding="utf-8")
    for path in COVERAGE_REPORT_PATHS:
        assert re.search(rf"^\s*mv \S+ site/{re.escape(path.rstrip('/'))}$", pages, re.M), (
            f"pages.yml does not move a report into site/{path}"
        )
    assert re.search(
        rf"diagrams/api_specs/generate\.py.*--swagger-ui-dir site/{re.escape(SWAGGER_SITE_PATH.rstrip('/'))}\b",
        pages,
    ), f"pages.yml does not build the Swagger UI consoles into site/{SWAGGER_SITE_PATH}"


def test_every_generated_spec_sheet_is_in_the_nav_and_nothing_else_under_api() -> None:
    """api/ nav entries and diagrams/api_specs/*.md are the same set, README included."""
    nav_api = {entry for entry in _load_nav() if entry.startswith(_API_PREFIX)}
    assert nav_api == _injected_api_pages()
    assert f"{_API_PREFIX}README.md" in nav_api, "the catalogue index serves /api/"


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
            elif path.startswith(_API_PREFIX):
                if not (_API_SPECS_DIR / path.removeprefix(_API_PREFIX)).is_file():
                    problems.append(f"{name} -> {target} (no matching generated spec sheet)")
            elif not (WIKI_DIR / path).is_file():
                problems.append(f"{name} -> {target} (not a wiki page)")
    assert not problems, f"unresolvable relative links: {problems}"


def test_wiki_uses_no_external_image_hosts() -> None:
    """Images come from the site itself, never from external hosts."""
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

    The content contract says deep documentation is linked via full GitHub
    URLs — this catches the natural authoring mistake.
    """
    offenders: list[str] = []
    for name, text in _wiki_pages().items():
        for target in _link_targets(text):
            if target.startswith(("docs/", "../docs/")):
                offenders.append(f"{name} -> {target}")
    assert not offenders, (
        f"relative docs/ links would 404 on the built site (use GitHub blob URLs): {offenders}"
    )


# =============================================================================
# The directory README (wiki/README.md): documentation, not a page
# =============================================================================

#: Files the README must describe, as the ``](<relative link>)`` targets it
#: uses for them (relative to wiki/). Each is a moving part of the build or
#: publish pipeline; a contributor who renames one has to come here, and to
#: the README, in the same change.
WIKI_README_SUPPORTING_FILES = (
    "../mkdocs.yml",
    "../scripts/mkdocs_hooks.py",
    "../scripts/preview_wiki.sh",
    "../.github/workflows/pages.yml",
    "../.github/workflows/lint.yml",
    "../.github/scripts/render_coverage_badges.py",
    "../tests/test_wiki.py",
    "../tests/test_docs_coverage.py",
    "../tests/test_markdown_links.py",
)


def test_wiki_readme_is_excluded_from_the_build_and_the_nav() -> None:
    """The README is for GitHub; index.md is the home page.

    Without the exclusion MkDocs warns that README.md conflicts with index.md
    and ``strict`` turns that into a failed build, so this is what keeps the
    strict build green rather than a stylistic preference.
    """
    config = yaml.safe_load(MKDOCS_YML.read_text(encoding="utf-8"))
    patterns = str(config.get("exclude_docs", "")).split()
    assert "/README.md" in patterns, "mkdocs.yml must exclude wiki/README.md via exclude_docs"
    assert "README.md" not in _load_nav(), "the directory README is not a wiki page"
    assert WIKI_README.is_file()


def test_wiki_readme_documents_every_page_in_the_directory() -> None:
    """Each published page has a row that links to it; no row names a ghost page."""
    text = WIKI_README.read_text(encoding="utf-8")
    linked_pages = {
        target for target in _link_targets(text) if target.endswith(".md") and "/" not in target
    }
    pages = set(_wiki_pages())
    assert linked_pages == pages, (
        f"README/pages mismatch — pages without a README link: {sorted(pages - linked_pages)}, "
        f"README links to pages that do not exist: {sorted(linked_pages - pages)}"
    )
    # Every published page is a table row (``| [`name.md`](name.md) |``), not
    # just a passing mention, so the table of contents stays a complete
    # inventory of the directory.
    for page in sorted(pages):
        assert re.search(rf"^\| \[`{re.escape(page)}`\]\({re.escape(page)}\) \|", text, re.M), (
            f"wiki/README.md has no table row for {page}"
        )


def test_wiki_readme_links_every_supporting_file() -> None:
    """The build and publish pipeline it describes is linked, not just named.

    The paths themselves resolve (and are checked) in tests/test_markdown_links.py;
    this pins that the README keeps pointing at each moving part at all.
    """
    targets = _link_targets(WIKI_README.read_text(encoding="utf-8"))
    missing = [path for path in WIKI_README_SUPPORTING_FILES if path not in targets]
    assert not missing, f"wiki/README.md no longer links to: {missing}"
    for path in WIKI_README_SUPPORTING_FILES:
        assert (WIKI_DIR / path).resolve().is_file(), f"supporting file vanished: {path}"
