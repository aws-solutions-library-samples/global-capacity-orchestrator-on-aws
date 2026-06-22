"""Example manifest discovery MCP tool.

Wraps the ``EXAMPLE_METADATA`` catalog defined in ``gco_mcp/resources/docs.py``
and exposes a single ``find_examples`` tool that the LLM can call with a
free-text query plus optional category/gpu/opt_in filters. Scoring is a
deterministic weighted sum over keyword/summary/name/use_case substring
matches; results are sorted by score descending then name ascending so a
caller iterating with ``limit`` always sees a stable ordering.
"""

from audit import audit_logged
from resources.docs import EXAMPLE_METADATA
from server import mcp


def _coerce_bool_flag(value: str | bool | None) -> bool | None:
    """Normalise a string/bool tri-state filter to True/False/None.

    Returns ``None`` when the caller did not supply a filter (so the
    search loop knows to skip the gpu/opt_in checks entirely).
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    return value.strip().lower() in ("yes", "true", "1")


def _has_gpu(meta: dict[str, str | list[str]]) -> bool:
    """Treat anything other than ``"no"``/``""`` as GPU-bearing.

    The ``gpu`` field carries values like ``"NVIDIA"``, ``"NVIDIA + EFA"``,
    ``"Trainium"``, ``"Inferentia"``, ``"NVIDIA (time-sliced)"``, and
    ``"optional"`` — all of which should match ``gpu="yes"``.
    """
    return str(meta.get("gpu", "no")) not in ("", "no")


def _search(
    query: str | None,
    category: str | None,
    gpu: str | bool | None,
    opt_in: str | bool | None,
) -> list[tuple[str, int]]:
    """Filter and score examples; return ``[(name, score), ...]`` sorted desc."""
    want_gpu = _coerce_bool_flag(gpu)
    want_opt_in = _coerce_bool_flag(opt_in)
    q = query.lower() if query else None

    results: list[tuple[str, int]] = []
    for name, meta in EXAMPLE_METADATA.items():
        # Hard filters — drop the entry when any non-matching filter is set.
        if category and str(meta.get("category", "")).lower() != category.lower():
            continue
        if want_gpu is not None and _has_gpu(meta) != want_gpu:
            continue
        if want_opt_in is not None and bool(meta.get("opt_in", "")) != want_opt_in:
            continue

        # Scoring runs only when there's a query — without one, every entry
        # that survived the filters is included with score 0.
        score = 0
        if q:
            keywords = meta.get("keywords", [])
            if isinstance(keywords, list):
                for kw in keywords:
                    if q in str(kw).lower():
                        score += 5
            if q in str(meta.get("summary", "")).lower():
                score += 2
            if q in name.lower():
                score += 3
            use_cases = meta.get("use_cases", [])
            if isinstance(use_cases, list):
                for uc in use_cases:
                    if q in str(uc).lower():
                        score += 3
            if score == 0:
                continue
        results.append((name, score))

    # Sort by score desc, then name asc for stable ordering across calls.
    results.sort(key=lambda x: (-x[1], x[0]))
    return results


def _format(name: str) -> dict[str, object]:
    """Format a metadata entry for the tool response."""
    meta = EXAMPLE_METADATA.get(name, {})
    return {
        "name": name,
        "category": meta.get("category", ""),
        "summary": meta.get("summary", ""),
        "gpu": meta.get("gpu", "no"),
        "opt_in": meta.get("opt_in", ""),
        "submission": meta.get("submission", ""),
        "keywords": meta.get("keywords", []),
        "use_cases": meta.get("use_cases", []),
        "related": meta.get("related", []),
    }


@mcp.tool(tags={"safe", "examples"})
@audit_logged
async def find_examples(
    query: str | None = None,
    category: str | None = None,
    gpu: str | None = None,
    opt_in: str | None = None,
    limit: int = 10,
) -> list[dict[str, object]]:
    """`find_examples` — search the example-manifest catalog by keyword and filters.

    Args:
        query: Natural-language query matched against keywords, summary,
            name, and use_cases (case-insensitive substring match).
        category: Filter by category (case-insensitive exact match).
        gpu: Pass ``"yes"``/``"true"`` to require GPU examples;
            ``"no"``/``"false"`` to exclude. Omit to leave unconstrained.
        opt_in: Pass ``"yes"`` to require an opt-in feature flag;
            ``"no"`` to exclude.
        limit: Maximum results (default 10). ``limit <= 0`` returns ``[]``.

    Returns a list of dicts with ``name``, ``category``, ``summary``,
    ``gpu``, ``opt_in``, ``submission``, ``keywords``, ``use_cases``, and
    ``related`` for each match.
    """
    if limit <= 0:
        return []

    no_filters = not query and not category and gpu is None and opt_in is None
    if no_filters:
        # Stable alpha-sorted listing for the no-arg case.
        names = sorted(EXAMPLE_METADATA.keys())[:limit]
        return [_format(name) for name in names]

    matches = _search(query, category, gpu, opt_in)
    return [_format(name) for name, _score in matches[:limit]]
