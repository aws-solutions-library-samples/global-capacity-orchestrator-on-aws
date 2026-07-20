"""
Tests for the GCO MCP server's docs-discovery surface.

Covers the three searchable catalogs: ``DOC_METADATA`` for ``docs/*.md``,
``ROOT_DOC_METADATA`` for normative project-root documents, and
``PACKAGE_DOC_METADATA`` for package READMEs. Tests enforce file/catalog
symmetry where applicable, pairwise-disjoint keys, related-reference closure,
``find_docs`` discovery and resource URIs, and the topic/related resource paths.
"""

import asyncio
import sys
from pathlib import Path

from hypothesis import given, settings
from hypothesis import strategies as st

# Ensure gco_mcp/ is importable
sys.path.insert(0, str(Path(__file__).parent.parent / "gco_mcp"))

import run_mcp  # noqa: E402, F401  -- side effect: registers tools and resources
from resources.docs import (  # noqa: E402
    DOC_METADATA,
    DOCS_DIR,
    PACKAGE_DOC_METADATA,
    PROJECT_ROOT,
    ROOT_DOC_METADATA,
)
from tools.docs import find_docs  # noqa: E402

# Pull the shared FastMCP instance with everything registered from
# ``run_mcp`` rather than ``server`` because importing ``server`` alone
# leaves the resource handlers unregistered.
mcp = run_mcp.mcp


# =============================================================================
# DOC_METADATA structural invariants
# =============================================================================


@settings(max_examples=100, derandomize=True)
@given(name=st.sampled_from(sorted(DOC_METADATA.keys())))
def test_every_doc_has_md_file_property(name: str) -> None:
    """Every metadata key points at a real markdown file under docs/."""
    assert (DOCS_DIR / f"{name}.md").is_file(), f"missing docs/{name}.md"


def test_metadata_keys_match_md_files() -> None:
    """Symmetric check: every .md file has a metadata entry, and vice versa."""
    md_names = {f.stem for f in DOCS_DIR.glob("*.md")}
    metadata_names = set(DOC_METADATA.keys())
    assert md_names == metadata_names, (
        f"Metadata/markdown mismatch — only in markdown: {md_names - metadata_names}, "
        f"only in metadata: {metadata_names - md_names}"
    )


def test_every_doc_related_reference_resolves() -> None:
    """Every entry in any ``related`` list must itself be a key in DOC_METADATA."""
    keys = set(DOC_METADATA.keys())
    for name, meta in DOC_METADATA.items():
        related = meta.get("related", [])
        assert isinstance(related, list), f"{name!r}.related must be a list"
        for ref in related:
            assert ref in keys, f"{name!r}.related references unknown {ref!r}"


# =============================================================================
# ROOT_DOC_METADATA structural invariants
# =============================================================================


@settings(max_examples=100, derandomize=True)
@given(name=st.sampled_from(sorted(ROOT_DOC_METADATA.keys())))
def test_every_root_doc_has_file_property(name: str) -> None:
    """Every root-doc entry points at its declared project-root Markdown file."""
    rel_path = ROOT_DOC_METADATA[name]["path"]
    assert isinstance(rel_path, str)
    assert (PROJECT_ROOT / rel_path).is_file(), f"missing {rel_path}"


def test_doc_catalog_keys_are_pairwise_disjoint() -> None:
    """Merged search catalogs must never overwrite an identically named entry."""
    catalogs = {
        "docs": set(DOC_METADATA),
        "root": set(ROOT_DOC_METADATA),
        "package": set(PACKAGE_DOC_METADATA),
    }
    names = list(catalogs)
    for index, left in enumerate(names):
        for right in names[index + 1 :]:
            overlap = catalogs[left] & catalogs[right]
            assert not overlap, f"{left}/{right} catalog key collision: {overlap}"


def test_every_root_doc_related_reference_resolves() -> None:
    """Root-doc relations resolve in the merged searchable catalog."""
    union = set(DOC_METADATA) | set(ROOT_DOC_METADATA) | set(PACKAGE_DOC_METADATA)
    for name, meta in ROOT_DOC_METADATA.items():
        related = meta.get("related", [])
        assert isinstance(related, list), f"{name!r}.related must be a list"
        for ref in related:
            assert ref in union, f"{name!r}.related references unknown {ref!r}"


def test_find_docs_surfaces_tenets_with_static_resource_uri() -> None:
    """The root tenets are searchable and point to their static resource."""
    results = asyncio.run(find_docs(query="north star", limit=50))
    by_name = {r["name"]: r for r in results}
    assert "TENETS" in by_name
    assert by_name["TENETS"]["resource_uri"] == "docs://gco/TENETS"


def test_tenets_resource_serves_toc_and_metadata_header() -> None:
    """The static TENETS resource returns the normative root document."""
    result = asyncio.run(mcp.read_resource("docs://gco/TENETS"))
    content = result.contents[0].content
    assert "<!-- Topics:" in content
    assert "# GCO Tenets" in content
    assert "## Table of Contents" in content
    assert "## North Star" in content


# =============================================================================
# PACKAGE_DOC_METADATA structural invariants
# =============================================================================


@settings(max_examples=100, derandomize=True)
@given(name=st.sampled_from(sorted(PACKAGE_DOC_METADATA.keys())))
def test_every_package_doc_has_readme_file_property(name: str) -> None:
    """Every package-doc entry points at a real README under the project root."""
    rel_path = PACKAGE_DOC_METADATA[name]["path"]
    assert isinstance(rel_path, str)
    assert (PROJECT_ROOT / rel_path).is_file(), f"missing {rel_path}"


def test_package_doc_keys_are_disjoint_from_doc_metadata() -> None:
    """Package keys remain disjoint from both uppercase guide catalogs."""
    overlap = (set(DOC_METADATA) | set(ROOT_DOC_METADATA)) & set(PACKAGE_DOC_METADATA)
    assert not overlap, f"catalog key collision: {overlap}"


def test_every_package_doc_related_reference_resolves() -> None:
    """Each package-doc ``related`` entry resolves in either catalog (the union)."""
    union = set(DOC_METADATA) | set(ROOT_DOC_METADATA) | set(PACKAGE_DOC_METADATA)
    for name, meta in PACKAGE_DOC_METADATA.items():
        related = meta.get("related", [])
        assert isinstance(related, list), f"{name!r}.related must be a list"
        for ref in related:
            assert ref in union, f"{name!r}.related references unknown {ref!r}"


def test_find_docs_surfaces_package_docs_with_resource_uri() -> None:
    """A package README is findable and carries its ``docs://gco/packages`` URI."""
    results = asyncio.run(find_docs(query="metric reader", limit=50))
    by_name = {r["name"]: r for r in results}
    assert "mcp-metric-readers" in by_name, "package README not surfaced by find_docs"
    assert by_name["mcp-metric-readers"]["resource_uri"] == (
        "docs://gco/packages/mcp-metric-readers"
    )


def test_find_docs_guide_results_carry_docs_resource_uri() -> None:
    """A ``docs/*.md`` guide result carries its ``docs://gco/docs`` URI."""
    results = asyncio.run(find_docs(topic="architecture", limit=50))
    architecture = next((r for r in results if r["name"] == "ARCHITECTURE"), None)
    assert architecture is not None
    assert architecture["resource_uri"] == "docs://gco/docs/ARCHITECTURE"


def test_package_doc_resource_serves_readme_with_header() -> None:
    """The packages resource returns README content with a Topics header."""
    result = asyncio.run(mcp.read_resource("docs://gco/packages/mcp-mission-judge"))
    content = result.contents[0].content
    assert "<!-- Topics:" in content
    assert "Mission Judge" in content


def test_package_doc_resource_unknown_returns_available_list() -> None:
    """Unknown slug returns the literal "Package doc 'X' not found." string."""
    result = asyncio.run(mcp.read_resource("docs://gco/packages/nonexistent"))
    content = result.contents[0].content
    assert "not found" in content
    assert "Available:" in content


# =============================================================================
# find_docs behaviour
# =============================================================================


@settings(max_examples=200)
@given(data=st.data())
def test_topic_match_property(data: st.DataObject) -> None:
    """If a topic exists in any doc's topics list, querying that topic
    returns the doc.
    """
    candidates = [
        name
        for name, meta in DOC_METADATA.items()
        if isinstance(meta.get("topics", []), list) and meta.get("topics")
    ]
    if not candidates:
        return  # Nothing to test
    name = data.draw(st.sampled_from(candidates))
    topics = DOC_METADATA[name]["topics"]
    assert isinstance(topics, list)
    topic = data.draw(st.sampled_from(topics))

    results = asyncio.run(find_docs(topic=str(topic), limit=len(DOC_METADATA)))
    result_names = [r["name"] for r in results]
    assert name in result_names, f"querying topic {topic!r} did not return {name!r}"


def test_find_docs_no_args_returns_alpha_sorted_first_limit() -> None:
    """With no filters and no query, the merged catalog is sorted and clipped."""
    results = asyncio.run(find_docs(limit=5))
    assert len(results) == 5
    names = [r["name"] for r in results]
    all_names = set(DOC_METADATA) | set(ROOT_DOC_METADATA) | set(PACKAGE_DOC_METADATA)
    assert names == sorted(all_names)[:5]


# =============================================================================
# Resource paths
# =============================================================================


def test_docs_by_topic_unknown_returns_available_list() -> None:
    """Unknown topic returns the literal "Topic 'X' not found." string."""
    result = asyncio.run(mcp.read_resource("docs://gco/docs/by-topic/nonexistent"))
    content = result.contents[0].content
    assert "not found" in content
    assert "Available:" in content


def test_docs_by_related_unknown_returns_available_list() -> None:
    """Unknown doc name returns the literal "Doc 'X' not found." string."""
    result = asyncio.run(mcp.read_resource("docs://gco/docs/by-related/nonexistent"))
    content = result.contents[0].content
    assert "not found" in content
    assert "Available:" in content
