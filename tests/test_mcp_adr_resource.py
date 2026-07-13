"""Tests for the directory-driven ADR resources on the GCO MCP server.

Covers the ``docs://gco/adr/index`` listing and the ``docs://gco/adr/{id}``
per-record resource: numeric-id / filename-stem / guide resolution, the
not-found message, path-traversal rejection, metadata parsing (title +
status), and the directory-driven invariant that any ``NNNN-*.md`` record
shows up in the index with no per-file registration. Also asserts the ADR
resources are advertised from the top-level ``docs://gco/index``.
"""

import asyncio
import sys
from pathlib import Path

# Ensure gco_mcp/ is importable, mirroring the other MCP test modules.
sys.path.insert(0, str(Path(__file__).parent.parent / "gco_mcp"))

import run_mcp  # noqa: E402  -- side effect: registers tools and resources
from resources import docs as docs_mod  # noqa: E402

mcp = run_mcp.mcp

_ADR_0001 = "0001-record-architecture-decisions"


def _read(uri: str) -> str:
    """Read a registered resource by URI and return its text content."""
    result = asyncio.run(mcp.read_resource(uri))
    return result.contents[0].content


# ---------------------------------------------------------------------------
# Record discovery and metadata parsing
# ---------------------------------------------------------------------------


def test_record_files_exclude_guides() -> None:
    """Only ``NNNN-*.md`` files are records; README and template are guides."""
    names = {p.name for p in docs_mod._adr_record_files()}
    assert f"{_ADR_0001}.md" in names
    assert "README.md" not in names
    assert "template.md" not in names


def test_parse_adr_extracts_title_and_status() -> None:
    """The numeric heading prefix is stripped and the status line is read."""
    meta = docs_mod._parse_adr(docs_mod.ADR_DIR / f"{_ADR_0001}.md")
    assert meta["title"] == "Record architecture decisions"
    assert meta["status"] == "Accepted"


# ---------------------------------------------------------------------------
# Index resource
# ---------------------------------------------------------------------------


def test_index_lists_every_record() -> None:
    content = _read("docs://gco/adr/index")
    for path in docs_mod._adr_record_files():
        assert f"docs://gco/adr/{path.name[:4]}" in content
    assert "Record architecture decisions" in content
    assert "Accepted" in content


def test_index_is_directory_driven(monkeypatch, tmp_path) -> None:
    """A record dropped on disk appears in the index with no code change."""
    adr = tmp_path / "adr"
    adr.mkdir()
    (adr / "0002-example-decision.md").write_text(
        "# 0002. Example decision\n\n- **Status:** Proposed\n\n## Context\n\nx\n",
        encoding="utf-8",
    )
    (adr / "0005-later-decision.md").write_text(
        "# 0005. Later decision\n\n- **Status:** Accepted\n\n## Context\n\nx\n",
        encoding="utf-8",
    )
    # Guides must be ignored by the record listing.
    (adr / "README.md").write_text("# Architecture Decision Records\n", encoding="utf-8")
    (adr / "template.md").write_text("# NNNN. Title\n", encoding="utf-8")
    monkeypatch.setattr(docs_mod, "ADR_DIR", adr)

    assert [p.name for p in docs_mod._adr_record_files()] == [
        "0002-example-decision.md",
        "0005-later-decision.md",
    ]
    out = docs_mod.adr_index_resource()
    assert "docs://gco/adr/0002" in out
    assert "docs://gco/adr/0005" in out
    assert "Example decision" in out
    assert "Later decision" in out


def test_index_empty_directory_is_graceful(monkeypatch, tmp_path) -> None:
    """An ADR directory with no records still renders (no records yet)."""
    adr = tmp_path / "adr"
    adr.mkdir()
    monkeypatch.setattr(docs_mod, "ADR_DIR", adr)
    out = docs_mod.adr_index_resource()
    assert "No ADRs" in out


# ---------------------------------------------------------------------------
# Single-record resource
# ---------------------------------------------------------------------------


def test_resource_by_numeric_id() -> None:
    assert _read("docs://gco/adr/0001").startswith("# 0001. Record architecture decisions")


def test_resource_by_full_stem() -> None:
    assert "# 0001. Record architecture decisions" in _read(f"docs://gco/adr/{_ADR_0001}")


def test_resource_serves_guides() -> None:
    assert "Architecture Decision Records" in _read("docs://gco/adr/README")
    assert _read("docs://gco/adr/template").startswith("# NNNN.")


def test_short_numeric_id_resolves() -> None:
    assert docs_mod._resolve_adr("1") == docs_mod.ADR_DIR / f"{_ADR_0001}.md"


def test_unknown_id_returns_not_found_with_available() -> None:
    content = _read("docs://gco/adr/9999")
    assert "not found" in content
    assert "0001" in content


# ---------------------------------------------------------------------------
# Security: path traversal
# ---------------------------------------------------------------------------


def test_path_traversal_is_rejected() -> None:
    assert docs_mod._resolve_adr("../README") is None
    assert docs_mod._resolve_adr("../../gco_mcp/server") is None
    assert docs_mod._resolve_adr("foo/bar") is None
    assert docs_mod._resolve_adr("") is None
    assert docs_mod.adr_resource("../README").startswith("ADR '../README' not found")


# ---------------------------------------------------------------------------
# Discoverability from the top-level index
# ---------------------------------------------------------------------------


def test_docs_index_advertises_adr_resources() -> None:
    index = docs_mod.docs_index()
    assert "Architecture Decision Records" in index
    assert "docs://gco/adr/index" in index
    assert "docs://gco/adr/{id}" in index
