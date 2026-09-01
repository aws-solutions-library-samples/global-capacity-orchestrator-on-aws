"""Tests for gco_mcp/completions.py — MCP argument completion (FastMCP 4).

Covers the completion handler end-to-end through the in-memory FastMCP
client (``completion/complete`` requests against the live registry) and
the matching/dispatch helpers directly.
"""

import sys
from pathlib import Path

import pytest
from mcp_types import CompletionArgument, PromptReference, ResourceTemplateReference

sys.path.insert(0, str(Path(__file__).parent.parent / "gco_mcp"))

import run_mcp  # noqa: E402  -- side-effect registers tools/resources/completions
from completions import (  # noqa: E402
    _MAX_COMPLETIONS,
    _complete_argument,
    _match,
)
from resources.docs import DOC_METADATA, EXAMPLE_METADATA  # noqa: E402


def _template_ref(uri: str) -> ResourceTemplateReference:
    return ResourceTemplateReference(type="ref/resource", uri=uri)


def _argument(name: str, value: str) -> CompletionArgument:
    return CompletionArgument(name=name, value=value)


# ---------------------------------------------------------------------------
# _match ranking
# ---------------------------------------------------------------------------


class TestMatch:
    def test_empty_partial_returns_all_capped(self) -> None:
        candidates = [f"c{i}" for i in range(150)]
        out = _match(candidates, "")
        assert out == candidates[:_MAX_COMPLETIONS]

    def test_prefix_matches_rank_before_substring_matches(self) -> None:
        candidates = ["CLUSTER_ARCH", "ARCHITECTURE", "OTHER"]
        out = _match(candidates, "arch")
        assert out == ["ARCHITECTURE", "CLUSTER_ARCH"]

    def test_matching_is_case_insensitive(self) -> None:
        assert _match(["Alpha", "beta"], "ALP") == ["Alpha"]

    def test_no_matches_returns_empty(self) -> None:
        assert _match(["Alpha"], "zzz") == []


# ---------------------------------------------------------------------------
# Handler dispatch (direct calls)
# ---------------------------------------------------------------------------


class TestHandlerDispatch:
    async def test_doc_name_completion_uses_doc_registry(self) -> None:
        out = await _complete_argument(
            _template_ref("docs://gco/docs/{doc_name}"), _argument("doc_name", ""), None
        )
        assert out == sorted(DOC_METADATA)[:_MAX_COMPLETIONS]

    async def test_example_name_completion_uses_example_registry(self) -> None:
        out = await _complete_argument(
            _template_ref("docs://gco/examples/{example_name}"),
            _argument("example_name", ""),
            None,
        )
        assert out == sorted(EXAMPLE_METADATA)[:_MAX_COMPLETIONS]

    async def test_category_completion_derives_unique_categories(self) -> None:
        out = await _complete_argument(
            _template_ref("docs://gco/examples/by-category/{category}"),
            _argument("category", ""),
            None,
        )
        assert out
        assert out == sorted(set(out))
        expected = {str(m["category"]) for m in EXAMPLE_METADATA.values() if m.get("category")}
        assert set(out) == expected

    async def test_adr_completion_lists_numbered_records(self) -> None:
        out = await _complete_argument(
            _template_ref("docs://gco/adr/{adr_id}"), _argument("adr_id", ""), None
        )
        assert out
        assert all(stem[:4].isdigit() for stem in out)

    async def test_config_filename_completion_uses_allowlist(self) -> None:
        from resources.source import _CONFIG_FILES

        out = await _complete_argument(
            _template_ref("source://gco/config/{filename}"), _argument("filename", ""), None
        )
        assert out == sorted(_CONFIG_FILES)

    async def test_unknown_template_returns_none(self) -> None:
        out = await _complete_argument(
            _template_ref("gco://jobs/{job_name}"), _argument("job_name", "x"), None
        )
        assert out is None

    async def test_unknown_argument_name_returns_none(self) -> None:
        out = await _complete_argument(
            _template_ref("docs://gco/docs/{doc_name}"), _argument("other_arg", "x"), None
        )
        assert out is None

    async def test_prompt_reference_returns_none(self) -> None:
        # This server registers no prompts; a PromptReference has no ``uri``
        # attribute so the handler declines rather than raising.
        out = await _complete_argument(
            PromptReference(type="ref/prompt", name="some-prompt"),
            _argument("arg", "x"),
            None,
        )
        assert out is None

    async def test_provider_exception_degrades_to_none(self, monkeypatch) -> None:
        import completions as completions_mod

        def _boom() -> list[str]:
            raise RuntimeError("registry unavailable")

        monkeypatch.setitem(
            completions_mod._TEMPLATE_ARG_SOURCES,
            ("docs://gco/docs/{doc_name}", "doc_name"),
            _boom,
        )
        out = await _complete_argument(
            _template_ref("docs://gco/docs/{doc_name}"), _argument("doc_name", "A"), None
        )
        assert out is None


# ---------------------------------------------------------------------------
# End-to-end: completion/complete over the in-memory client
# ---------------------------------------------------------------------------


class TestCompletionEndToEnd:
    @pytest.mark.asyncio
    async def test_doc_name_prefix_completion_round_trip(self) -> None:
        from fastmcp import Client

        async with Client(run_mcp.mcp) as client:
            completion = await client.complete(
                _template_ref("docs://gco/docs/{doc_name}"),
                {"name": "doc_name", "value": "ARCH"},
            )
        assert "ARCHITECTURE" in completion.values
        assert completion.values[0] == "ARCHITECTURE"

    @pytest.mark.asyncio
    async def test_registration_advertises_completions_capability(self) -> None:
        from fastmcp import Client

        async with Client(run_mcp.mcp) as client:
            caps = client.server_capabilities
        assert caps is not None
        assert getattr(caps, "completions", None) is not None
