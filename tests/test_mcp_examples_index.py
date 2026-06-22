"""
Tests for the GCO MCP server's example-discovery surface.

Covers ``EXAMPLE_METADATA`` consistency (every metadata key resolves to a
real ``examples/*.yaml`` file and every YAML file has a metadata entry),
the ``related`` reference closure, the ``find_examples`` tool's keyword
matching and edge cases (no-arg listing, non-positive limits), and the
two new ``docs://gco/examples/by-category/...`` and
``docs://gco/examples/by-use-case/...`` resource paths.
"""

import asyncio
import sys
from pathlib import Path

from hypothesis import given, settings
from hypothesis import strategies as st

# Ensure gco_mcp/ is importable
sys.path.insert(0, str(Path(__file__).parent.parent / "gco_mcp"))

import run_mcp  # noqa: E402, F401  -- side effect: registers tools and resources
from resources.docs import EXAMPLE_METADATA, EXAMPLES_DIR  # noqa: E402
from tools.examples import find_examples  # noqa: E402

# The shared FastMCP instance with everything registered. Pulled from
# ``run_mcp`` rather than ``server`` because importing ``server`` alone
# leaves the resource handlers unregistered.
mcp = run_mcp.mcp


# =============================================================================
# EXAMPLE_METADATA structural invariants (Tasks 7.7, 7.8)
# =============================================================================


@settings(max_examples=100, derandomize=True)
@given(name=st.sampled_from(sorted(EXAMPLE_METADATA.keys())))
def test_every_example_has_yaml_property(name: str) -> None:
    """Every metadata key points at a real YAML file under examples/."""
    assert (EXAMPLES_DIR / f"{name}.yaml").is_file(), f"missing examples/{name}.yaml"


def test_metadata_keys_match_yaml_files() -> None:
    """Symmetric check: every YAML file has a metadata entry, and vice versa."""
    yaml_names = {f.stem for f in EXAMPLES_DIR.glob("*.yaml")}
    metadata_names = set(EXAMPLE_METADATA.keys())
    assert yaml_names == metadata_names, (
        f"Metadata/YAML mismatch — only in YAML: {yaml_names - metadata_names}, "
        f"only in metadata: {metadata_names - yaml_names}"
    )


def test_every_related_reference_resolves() -> None:
    """Every entry in any ``related`` list must itself be a key in EXAMPLE_METADATA."""
    keys = set(EXAMPLE_METADATA.keys())
    for name, meta in EXAMPLE_METADATA.items():
        related = meta.get("related", [])
        assert isinstance(related, list), f"{name!r}.related must be a list"
        for ref in related:
            assert ref in keys, f"{name!r}.related references unknown {ref!r}"


# =============================================================================
# find_examples behavior (Tasks 7.9, 7.10)
# =============================================================================


@settings(max_examples=200)
@given(data=st.data())
def test_keyword_match_property(data: st.DataObject) -> None:
    """If a keyword exists in any example's keywords list, querying that
    keyword returns the example.
    """
    candidates = [
        name
        for name, meta in EXAMPLE_METADATA.items()
        if isinstance(meta.get("keywords", []), list) and meta.get("keywords")
    ]
    if not candidates:
        return  # Nothing to test
    name = data.draw(st.sampled_from(candidates))
    keywords = EXAMPLE_METADATA[name]["keywords"]
    assert isinstance(keywords, list)
    keyword = data.draw(st.sampled_from(keywords))

    results = asyncio.run(find_examples(query=str(keyword)))
    result_names = [r["name"] for r in results]
    assert name in result_names, f"querying {keyword!r} did not return {name!r}"


def test_find_examples_no_args_returns_alpha_sorted_first_limit() -> None:
    """With no filters and no query, the catalog is alpha-sorted and clipped."""
    results = asyncio.run(find_examples(limit=5))
    assert len(results) == 5
    names = [r["name"] for r in results]
    assert names == sorted(EXAMPLE_METADATA.keys())[:5]


def test_find_examples_negative_limit_returns_empty() -> None:
    """``limit <= 0`` short-circuits to an empty list."""
    assert asyncio.run(find_examples(limit=-1)) == []
    assert asyncio.run(find_examples(limit=0)) == []


# =============================================================================
# Resource paths (Task 7.11)
# =============================================================================


def test_examples_by_category_unknown_returns_available_list() -> None:
    """Unknown category returns the literal "Category 'X' not found." string."""
    result = asyncio.run(mcp.read_resource("docs://gco/examples/by-category/nonexistent"))
    content = result.contents[0].content
    assert "not found" in content
    assert "Available:" in content


def test_examples_by_use_case_no_match_suggests_find_examples() -> None:
    """Unknown use_case returns a guiding pointer to ``find_examples``."""
    result = asyncio.run(
        mcp.read_resource("docs://gco/examples/by-use-case/totally-bogus-use-case")
    )
    content = result.contents[0].content
    assert "No examples match use case" in content
    assert "find_examples" in content


# =============================================================================
# Helper coverage — the small filter primitives in gco_mcp/tools/examples.py
# =============================================================================


def test_coerce_bool_flag_returns_none_for_none_input() -> None:
    """``None`` is the "filter not supplied" sentinel — it must round-trip."""
    from tools.examples import _coerce_bool_flag

    assert _coerce_bool_flag(None) is None


def test_coerce_bool_flag_passes_through_actual_bools() -> None:
    """Real ``bool`` values are returned verbatim — no string-mangling."""
    from tools.examples import _coerce_bool_flag

    assert _coerce_bool_flag(True) is True
    assert _coerce_bool_flag(False) is False


def test_coerce_bool_flag_recognises_truthy_strings() -> None:
    """``"yes"``, ``"true"``, and ``"1"`` (case-insensitive) parse as ``True``."""
    from tools.examples import _coerce_bool_flag

    for s in ("yes", "YES", "Yes", "true", "TRUE", "True", "1", " yes ", " 1\n"):
        assert _coerce_bool_flag(s) is True, f"{s!r} should coerce to True"


def test_coerce_bool_flag_rejects_other_strings_as_false() -> None:
    """Anything else (including ``"no"``, ``"false"``, ``""``) is ``False``."""
    from tools.examples import _coerce_bool_flag

    for s in ("no", "false", "0", "", "anything-else", "NO"):
        assert _coerce_bool_flag(s) is False, f"{s!r} should coerce to False"


def test_has_gpu_treats_no_and_empty_as_false() -> None:
    """``"no"`` and ``""`` are the only non-GPU values; everything else is GPU."""
    from tools.examples import _has_gpu

    assert _has_gpu({"gpu": "no"}) is False
    assert _has_gpu({"gpu": ""}) is False
    # Default to ``"no"`` when the key is missing entirely.
    assert _has_gpu({}) is False


def test_has_gpu_treats_any_other_string_as_true() -> None:
    """``"NVIDIA"``, ``"Trainium"``, ``"optional"`` all count as GPU-bearing."""
    from tools.examples import _has_gpu

    for value in ("NVIDIA", "NVIDIA + EFA", "Trainium", "Inferentia", "optional"):
        assert _has_gpu({"gpu": value}) is True, f"{value!r} should count as GPU"


def test_find_examples_gpu_filter_uses_has_gpu() -> None:
    """``gpu="yes"`` filters out non-GPU examples; ``gpu="no"`` keeps only those."""
    gpu_results = asyncio.run(find_examples(gpu="yes"))
    no_gpu_results = asyncio.run(find_examples(gpu="no"))
    # Both lists are non-empty (we have GPU and non-GPU examples).
    assert gpu_results
    assert no_gpu_results
    # The two sets are disjoint by construction.
    gpu_names = {r["name"] for r in gpu_results}
    no_gpu_names = {r["name"] for r in no_gpu_results}
    assert gpu_names.isdisjoint(no_gpu_names)


def test_find_examples_opt_in_filter_passes_through_to_search() -> None:
    """``opt_in="yes"`` and ``opt_in="no"`` partition the catalog disjointly."""
    opt_in_yes = {r["name"] for r in asyncio.run(find_examples(opt_in="yes"))}
    opt_in_no = {r["name"] for r in asyncio.run(find_examples(opt_in="no"))}
    # The two filters partition the catalog (every example has an
    # opt_in flag, true or false).
    assert opt_in_yes.isdisjoint(opt_in_no)


def test_find_examples_query_with_zero_score_drops_entry() -> None:
    """A query that doesn't match any keyword/summary/use_case skips the entry.

    Pins the ``if score == 0: continue`` branch in ``_search``: the
    filter loop computes a zero score for entries that pass the
    category/gpu/opt_in filters but match no query token, and those
    entries are silently dropped.
    """
    results = asyncio.run(find_examples(query="this-query-matches-nothing-at-all-zzz"))
    # No example contains this random token, so the result is empty.
    assert results == []


def test_find_examples_query_matches_use_case() -> None:
    """A query that hits a ``use_cases`` entry scores 3 and returns the example.

    Picks a real use_case from the metadata and verifies the
    use_cases scoring branch is reachable.
    """
    from resources.docs import EXAMPLE_METADATA

    # Find any example with a non-empty use_cases list.
    target_name = None
    target_use_case = None
    for name, meta in EXAMPLE_METADATA.items():
        use_cases = meta.get("use_cases")
        if isinstance(use_cases, list) and use_cases:
            target_name = name
            target_use_case = str(use_cases[0])
            break
    if target_name is None or target_use_case is None:
        # No examples with use_cases defined — skip rather than fail.
        # The catalog could shrink in the future; this test stays
        # honest about what it actually exercised.
        return
    results = asyncio.run(find_examples(query=target_use_case))
    names = {r["name"] for r in results}
    assert target_name in names, f"use_case {target_use_case!r} did not return {target_name!r}"
