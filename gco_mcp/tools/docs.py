"""Documentation discovery MCP tool.

Wraps two catalogs defined in ``gco_mcp/resources/docs.py`` — ``DOC_METADATA``
(the ``docs/*.md`` guides) and ``PACKAGE_DOC_METADATA`` (the package-level
READMEs that live next to the code under ``gco_mcp/``) — and exposes a single
``find_docs`` tool the LLM can call with a free-text query plus an optional
topic filter. The two catalogs are merged into one searchable view; each
result carries a ``resource_uri`` so the caller knows the exact resource to
fetch (``docs://gco/docs/{name}`` for a guide, ``docs://gco/packages/{name}``
for a package README). Scoring is a deterministic weighted sum of topic and
summary/name substring matches; results are sorted by score descending then
name ascending so callers iterating with ``limit`` always see a stable
ordering.
"""

from audit import audit_logged
from resources.docs import DOC_METADATA, PACKAGE_DOC_METADATA
from server import mcp


def _catalog() -> dict[str, dict[str, str | list[str]]]:
    """Return the merged doc catalog: ``docs/*.md`` guides plus package READMEs.

    The two catalogs use disjoint key spaces by construction —
    ``DOC_METADATA`` keys are uppercase doc stems (``ARCHITECTURE``) and
    ``PACKAGE_DOC_METADATA`` keys are lowercase slugs (``mcp-mission``) — so a
    plain merge never drops an entry.
    """
    return {**DOC_METADATA, **PACKAGE_DOC_METADATA}


def _resource_uri(name: str) -> str:
    """Map a catalog key to the resource URI that serves its content."""
    if name in PACKAGE_DOC_METADATA:
        return f"docs://gco/packages/{name}"
    return f"docs://gco/docs/{name}"


def _search(query: str | None, topic: str | None) -> list[tuple[str, int]]:
    """Filter and score docs; return ``[(name, score), ...]`` sorted desc."""
    results: list[tuple[str, int]] = []
    q = query.lower() if query else None
    t = topic.lower() if topic else None
    for name, meta in _catalog().items():
        score = 0
        if t:
            topics = meta.get("topics", [])
            if isinstance(topics, list):
                for top in topics:
                    if t in str(top).lower():
                        score += 3
            # Topic filter is a hard constraint — no match means drop the
            # entry, even if a query string would have matched the summary.
            if score == 0:
                continue
        if q:
            # Keyword matches are the strongest free-text signal — every
            # entry's ``keywords`` list is curated to surface terms a
            # user is likely to search for (e.g. "vllm", "odcr",
            # "global accelerator") even when those phrases don't appear
            # verbatim in the summary.
            keywords = meta.get("keywords", [])
            if isinstance(keywords, list):
                for kw in keywords:
                    if q in str(kw).lower():
                        score += 4
            summary = str(meta.get("summary", "")).lower()
            if q in summary:
                score += 1
            if q in name.lower():
                score += 1
            # When the only signal is a query and it didn't hit, drop it.
            if score == 0 and not t:
                continue
        results.append((name, score))
    results.sort(key=lambda x: (-x[1], x[0]))
    return results


def _format(name: str) -> dict[str, object]:
    """Format a metadata entry for the tool response."""
    meta = _catalog().get(name, {})
    return {
        "name": name,
        "resource_uri": _resource_uri(name),
        "summary": meta.get("summary", ""),
        "topics": meta.get("topics", []),
        "keywords": meta.get("keywords", []),
        "related": meta.get("related", []),
    }


@mcp.tool(tags={"safe", "docs"})
@audit_logged
async def find_docs(
    query: str | None = None,
    topic: str | None = None,
    limit: int = 10,
) -> list[dict[str, object]]:
    """`find_docs` — search the docs catalog by topic and free-text query.

    Searches both the ``docs/*.md`` guides and the package-level READMEs that
    live next to the code under ``gco_mcp/``. Each result carries a
    ``resource_uri`` naming the exact resource to fetch
    (``docs://gco/docs/{name}`` for a guide, ``docs://gco/packages/{name}``
    for a package README).

    Args:
        query: Free-text query matched against the doc's keywords, summary,
            and name (case-insensitive substring match).
        topic: Filter by topic substring (case-insensitive). Acts as a hard
            filter — entries without a topic match are dropped.
        limit: Maximum results (default 10). ``limit <= 0`` returns ``[]``.

    Scoring: topic substring matches contribute 3 pts each; keyword
    substring matches contribute 4 pts each; summary/name substring
    matches contribute 1 pt each. Returns the top ``limit`` matches
    sorted by score descending then name ascending.
    """
    if limit <= 0:
        return []
    no_filters = not query and not topic
    if no_filters:
        # Stable alpha-sorted listing for the no-arg case.
        names = sorted(_catalog().keys())[:limit]
        return [_format(name) for name in names]
    matches = _search(query, topic)
    return [_format(name) for name, _score in matches[:limit]]
