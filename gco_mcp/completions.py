"""Argument completion for GCO resource templates (FastMCP 4).

FastMCP 4 lets a server answer MCP ``completion/complete`` requests through a
single handler registered with ``mcp.add_completion_handler``. Registering the
handler is also what advertises the ``completions`` capability during
negotiation, so clients only send completion requests when this server can
answer them.

Scope is deliberately limited to the static, registry-backed template
parameters — documentation names, example manifests, ADR ids, package README
slugs, and the project-config allowlist. Those complete from in-memory
metadata the resource modules already maintain, so a completion request never
costs an AWS round-trip. Live-state templates (``gco://``, ``tasks://``,
``mission://``, ``costs://``, ``images://``) would need per-keystroke network
calls and deliberately return no suggestions.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

# The MCP protocol caps one completion response at 100 values; FastMCP
# truncates anyway, but capping here keeps the payload deterministic.
_MAX_COMPLETIONS = 100


def _doc_names() -> list[str]:
    from resources.docs import DOC_METADATA

    return sorted(DOC_METADATA)


def _package_names() -> list[str]:
    from resources.docs import PACKAGE_DOC_METADATA

    return sorted(PACKAGE_DOC_METADATA)


def _example_names() -> list[str]:
    from resources.docs import EXAMPLE_METADATA

    return sorted(EXAMPLE_METADATA)


def _example_categories() -> list[str]:
    from resources.docs import EXAMPLE_METADATA

    categories = {
        str(meta.get("category", "")) for meta in EXAMPLE_METADATA.values() if meta.get("category")
    }
    return sorted(categories)


def _doc_topics() -> list[str]:
    from resources.docs import DOC_METADATA

    topics: set[str] = set()
    for meta in DOC_METADATA.values():
        raw = meta.get("topics")
        if isinstance(raw, list):
            topics.update(str(topic) for topic in raw)
    return sorted(topics)


def _adr_ids() -> list[str]:
    from resources.docs import _adr_record_files

    return [path.stem for path in _adr_record_files()]


def _config_filenames() -> list[str]:
    from resources.source import _CONFIG_FILES

    return sorted(_CONFIG_FILES)


# One entry per completable template parameter: (template URI as registered,
# argument name) -> zero-argument provider returning the full candidate list.
_TEMPLATE_ARG_SOURCES: dict[tuple[str, str], Callable[[], list[str]]] = {
    ("docs://gco/docs/{doc_name}", "doc_name"): _doc_names,
    ("docs://gco/docs/by-related/{doc_name}", "doc_name"): _doc_names,
    ("docs://gco/docs/by-topic/{topic}", "topic"): _doc_topics,
    ("docs://gco/packages/{package_name}", "package_name"): _package_names,
    ("docs://gco/examples/{example_name}", "example_name"): _example_names,
    ("docs://gco/examples/by-category/{category}", "category"): _example_categories,
    ("docs://gco/adr/{adr_id}", "adr_id"): _adr_ids,
    ("source://gco/config/{filename}", "filename"): _config_filenames,
}


def _match(candidates: list[str], partial: str) -> list[str]:
    """Rank candidates for a partial value: prefix matches first, then substring."""
    if not partial:
        return candidates[:_MAX_COMPLETIONS]
    lowered = partial.lower()
    prefix = [c for c in candidates if c.lower().startswith(lowered)]
    contains = [c for c in candidates if lowered in c.lower() and c not in prefix]
    return (prefix + contains)[:_MAX_COMPLETIONS]


async def _complete_argument(ref: Any, argument: Any, context: Any) -> list[str] | None:
    """Answer one ``completion/complete`` request.

    ``ref`` is the SDK's ``PromptReference`` or ``ResourceTemplateReference``;
    only resource templates resolve here (this server registers no prompts).
    Unknown templates and arguments return ``None``, which FastMCP renders as
    an empty completion — never an error.
    """
    template_uri = getattr(ref, "uri", None)
    arg_name = getattr(argument, "name", None)
    if not isinstance(template_uri, str) or not isinstance(arg_name, str):
        return None
    provider = _TEMPLATE_ARG_SOURCES.get((template_uri, arg_name))
    if provider is None:
        return None
    try:
        candidates = provider()
    except Exception:  # noqa: BLE001 — a completion must never break a session
        return None
    partial = getattr(argument, "value", "") or ""
    return _match(candidates, str(partial))


def register_completions(mcp_instance: Any) -> None:
    """Register the argument-completion handler on the shared MCP server.

    Called from ``run_mcp.py`` after every resource module has registered
    (the providers read registries owned by those modules). Calling it again
    replaces the handler, so reload-driven re-registration is idempotent.
    """
    mcp_instance.add_completion_handler(_complete_argument)
