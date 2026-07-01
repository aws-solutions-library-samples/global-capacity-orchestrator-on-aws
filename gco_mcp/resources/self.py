"""Self-indexing resources (``mcp://gco/...``) for the GCO MCP server.

Four templates that surface the live MCP catalog through resource URIs
so introspection clients (and AI assistants) can list every registered
tool and resource template at a glance, plus the feature-flag map that
gates each gated tool. Always-on — no feature flag gates these.

* ``mcp://gco/tools/index`` — full tool index. Returns JSON shaped as
  ``{"tools": [{"name", "description", "tags", "source_path",
  "source_line", "gating_flag"}, ...]}``. ``source_path`` is project-
  root-relative; ``source_line`` is the 1-indexed first line of the
  wrapped function. ``gating_flag`` is the ``GCO_ENABLE_*`` constant
  that gates the tool, or ``null`` when the tool is always-on.
* ``mcp://gco/tools/{tool_name}`` — single-tool detail. Same shape as
  one element of the index. Raises :class:`fastmcp.exceptions.NotFoundError`
  for unknown names so the FastMCP error-handling middleware maps it to
  MCP error code ``-32002``.
* ``mcp://gco/resources/index`` — index of every static resource and
  resource template. Returns ``{"resources": [{"uri", "name",
  "description", "tags", "source_path", "source_line"}, ...],
  "templates": [{"uri_template", "name", "description", ...}, ...]}``.
* ``mcp://gco/feature-flags`` — the umbrella + per-tool flag table.
  Returns ``{"flags": [{"name", "default", "gated_tools": [...]},
  ...]}``. The ``gated_tools`` list is the static map below, kept in
  sync by hand with the ``if is_enabled(...)`` blocks at the top of
  each ``gco_mcp/tools/*.py`` module. The ``mission`` family lives in a
  module-level ``if`` so its nine tools all gate together; image and
  destructive tools use multiple-flag combinations.

Tool-name → flag inference uses a static ``_TOOL_GATING_TABLE`` rather
than re-parsing the source modules at request time. That table is
short, easy to keep in sync, and cheap to read; the alternative — AST-
walking each ``gco_mcp/tools/*.py`` module on every list call — would
either thrash the disk on every introspection or grow a layer of
caches we'd then have to invalidate. The map is exercised in
``tests/test_mcp_self_resources.py`` so any drift between it and the
real gating bodies trips a test failure rather than a silent
documentation lie.
"""

from __future__ import annotations

import inspect
import json
import sys
from pathlib import Path
from typing import Any, cast

from feature_flags import (
    ALL_FLAGS,
    FLAG_ALL_TOOLS,
    FLAG_CAPACITY_PURCHASE,
    FLAG_DESTRUCTIVE_OPERATIONS,
    FLAG_IMAGE_PUBLISH,
    FLAG_INFRASTRUCTURE_DEPLOY,
    FLAG_INFRASTRUCTURE_DESTROY,
    FLAG_MISSION,
    FLAG_MODEL_UPLOAD,
)

# Import the live FastMCP instance so the resource handlers can hit
# the same registry the rest of the server sees.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Project root used to build relative ``source_path`` strings.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent


# ---------------------------------------------------------------------------
# Static gating table — kept in sync with the per-module ``if`` blocks.
# ---------------------------------------------------------------------------

_TOOL_GATING_TABLE: dict[str, str] = {
    # gco_mcp/tools/capacity.py — module-level gate for purchase tools
    "reserve_capacity": FLAG_CAPACITY_PURCHASE,
    # gco_mcp/tools/models.py — model upload + destructive
    "models_upload": FLAG_MODEL_UPLOAD,
    "delete_model": FLAG_DESTRUCTIVE_OPERATIONS,
    # gco_mcp/tools/images.py — image-publish + destructive
    "images_build": FLAG_IMAGE_PUBLISH,
    "images_push": FLAG_IMAGE_PUBLISH,
    "images_delete_tag": FLAG_DESTRUCTIVE_OPERATIONS,
    "images_delete_repo": FLAG_DESTRUCTIVE_OPERATIONS,
    "images_cleanup": FLAG_DESTRUCTIVE_OPERATIONS,
    "images_prune": FLAG_DESTRUCTIVE_OPERATIONS,
    # gco_mcp/tools/stacks.py — deploy + destroy
    "deploy_stack": FLAG_INFRASTRUCTURE_DEPLOY,
    "deploy_all": FLAG_INFRASTRUCTURE_DEPLOY,
    "bootstrap_cdk": FLAG_INFRASTRUCTURE_DEPLOY,
    "destroy_stack": FLAG_INFRASTRUCTURE_DESTROY,
    "destroy_all": FLAG_INFRASTRUCTURE_DESTROY,
    # gco_mcp/tools/mission.py — module-level gate
    "mission_start": FLAG_MISSION,
    "mission_status": FLAG_MISSION,
    "mission_iterate": FLAG_MISSION,
    "mission_checkpoint": FLAG_MISSION,
    "mission_complete": FLAG_MISSION,
    "mission_abort": FLAG_MISSION,
    "mission_resume": FLAG_MISSION,
    "mission_history": FLAG_MISSION,
    "mission_list": FLAG_MISSION,
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_not_found(message: str) -> Exception:
    """Construct the best available "not found" exception.

    Prefers :class:`fastmcp.exceptions.NotFoundError` because the
    FastMCP error-handling middleware maps it to MCP error code
    ``-32002`` for resource reads. Falls back through ``ResourceError``
    and finally :class:`KeyError` so the resource layer still surfaces
    a structured not-found regardless of the FastMCP build in use.
    """
    try:
        from fastmcp.exceptions import NotFoundError

        return NotFoundError(message)
    except ImportError:
        pass
    try:
        from fastmcp.exceptions import ResourceError

        return ResourceError(message)
    except ImportError:
        pass
    return KeyError(message)


def _source_info_for_fn(fn: Any) -> tuple[str | None, int | None]:
    """Return (project-root-relative path, 1-indexed first line) for ``fn``.

    Walks :func:`inspect.unwrap` so the source location of the wrapped
    function is reported rather than the audit decorator's wrapper.
    Both halves can be ``None`` when the source is unavailable (built-
    ins, dynamically generated functions); the index handler emits
    ``null`` JSON for those cases.
    """
    try:
        target = inspect.unwrap(fn)
    except Exception:
        target = fn

    try:
        src_path = inspect.getsourcefile(target)
    except (TypeError, OSError):
        src_path = None

    # Resolve the first line number. Prefer the code object's
    # ``co_firstlineno``: it is always present for real Python functions
    # and lambdas and equals what ``inspect.getsourcelines`` reports for
    # them, but (unlike ``getsourcelines``) it needs no re-read of the
    # on-disk source. That robustness matters under pytest's assertion-
    # rewriting import hook, where re-reading the source of a function
    # defined in a rewritten module raises ``OSError`` on newer CPython
    # 3.14 patch releases and would otherwise drop the line to ``None``.
    # Fall back to ``getsourcelines`` for the rare callable that exposes
    # a source location but no ``__code__``, and to ``None`` for built-ins.
    src_lineno: int | None = None
    code = getattr(target, "__code__", None)
    co_firstlineno = getattr(code, "co_firstlineno", None)
    if isinstance(co_firstlineno, int):
        src_lineno = co_firstlineno
    else:
        try:
            _src_lines, src_lineno = inspect.getsourcelines(target)
        except (TypeError, OSError):
            src_lineno = None

    rel_path: str | None = None
    if src_path:
        try:
            rel_path = str(Path(src_path).resolve().relative_to(_PROJECT_ROOT))
        except ValueError:
            # Tool defined outside the project tree (e.g. site-
            # packages). Fall back to the absolute path.
            rel_path = src_path

    return rel_path, src_lineno


async def _list_tools_async() -> list[Any]:
    """Snapshot every registered tool, asynchronously.

    The catch-all keeps a transient FastMCP error from blowing up the
    introspection endpoint — an empty list is safer than a 500.
    """
    from server import mcp

    try:
        # ``_list_tools`` returns a ``Sequence[Tool]``; widen to
        # ``list[Any]`` for the JSON-projection helpers below.
        return list(await mcp._list_tools())
    except Exception:
        return []


async def _list_resources_async() -> tuple[list[Any], list[Any]]:
    """Snapshot static resources and resource templates, asynchronously."""
    from server import mcp

    try:
        # ``_list_resources`` and ``_list_resource_templates`` return
        # ``Sequence[Resource]`` and ``Sequence[ResourceTemplate]``
        # respectively; widen to ``list[Any]`` so the JSON-projection
        # helpers don't have to know FastMCP's concrete classes.
        resources = list(await mcp._list_resources())
    except Exception:
        resources = []
    try:
        templates = list(await mcp._list_resource_templates())
    except Exception:
        templates = []
    return resources, templates


def _tool_to_dict(tool: Any) -> dict[str, Any]:
    """Build the index entry shape from a FastMCP tool object."""
    src_path, src_line = _source_info_for_fn(getattr(tool, "fn", None))
    tags = getattr(tool, "tags", None) or set()
    return {
        "name": tool.name,
        "description": getattr(tool, "description", "") or "",
        "tags": sorted(str(t) for t in tags),
        "source_path": src_path,
        "source_line": src_line,
        "gating_flag": _TOOL_GATING_TABLE.get(tool.name),
    }


def _resource_to_dict(resource: Any) -> dict[str, Any]:
    """Build the index entry shape from a FastMCP static resource."""
    src_path, src_line = _source_info_for_fn(getattr(resource, "fn", None))
    tags = getattr(resource, "tags", None) or set()
    return {
        "uri": str(getattr(resource, "uri", "")),
        "name": getattr(resource, "name", "") or "",
        "description": getattr(resource, "description", "") or "",
        "tags": sorted(str(t) for t in tags),
        "source_path": src_path,
        "source_line": src_line,
    }


def _template_to_dict(template: Any) -> dict[str, Any]:
    """Build the index entry shape from a FastMCP resource template."""
    src_path, src_line = _source_info_for_fn(getattr(template, "fn", None))
    tags = getattr(template, "tags", None) or set()
    return {
        "uri_template": getattr(template, "uri_template", "") or "",
        "name": getattr(template, "name", "") or "",
        "description": getattr(template, "description", "") or "",
        "tags": sorted(str(t) for t in tags),
        "source_path": src_path,
        "source_line": src_line,
    }


# ---------------------------------------------------------------------------
# Resource handler bodies
# ---------------------------------------------------------------------------


async def _tools_index() -> str:
    """Return the full tool index as a JSON string."""
    tools = await _list_tools_async()
    payload = {"tools": [_tool_to_dict(t) for t in tools]}
    return json.dumps(payload, default=str)


async def _tool_detail(tool_name: str) -> str:
    """Return one tool's detail dict as JSON, or raise not-found."""
    for tool in await _list_tools_async():
        if tool.name == tool_name:
            return json.dumps(_tool_to_dict(tool), default=str)
    raise _make_not_found(f"tool {tool_name!r} is not registered")


async def _resources_index() -> str:
    """Return the full resource + template index as a JSON string."""
    resources, templates = await _list_resources_async()
    payload = {
        "resources": [_resource_to_dict(r) for r in resources],
        "templates": [_template_to_dict(t) for t in templates],
    }
    return json.dumps(payload, default=str)


async def _feature_flags() -> str:
    """Return the feature-flag table as a JSON string.

    Each entry carries the flag's name, its always-False default
    (gates default off until the operator opts in), and the list of
    tool names the flag gates. The umbrella flag ``GCO_ENABLE_ALL_TOOLS``
    appears with an empty ``gated_tools`` list because it overrides
    every per-tool flag.
    """
    by_flag: dict[str, list[str]] = {flag: [] for flag in ALL_FLAGS}
    for tool_name, flag in _TOOL_GATING_TABLE.items():
        if flag in by_flag:
            by_flag[flag].append(tool_name)

    flags_list: list[dict[str, Any]] = [
        {
            "name": FLAG_ALL_TOOLS,
            "default": False,
            "gated_tools": [],
        }
    ]
    for flag in ALL_FLAGS:
        flags_list.append(
            {
                "name": flag,
                "default": False,
                "gated_tools": sorted(by_flag.get(flag, [])),
            }
        )

    return json.dumps({"flags": flags_list}, default=str)


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def register(mcp_instance: Any) -> None:
    """Register the four self-indexing resource handlers.

    Always-on. The handlers are pure functions of the live FastMCP
    registry plus the static gating table above, so registering them
    on import has no side effects beyond exposing the URIs.
    """
    mcp_instance.resource("mcp://gco/tools/index")(_tools_index)
    mcp_instance.resource("mcp://gco/tools/{tool_name}")(_tool_detail)
    mcp_instance.resource("mcp://gco/resources/index")(_resources_index)
    mcp_instance.resource("mcp://gco/feature-flags")(_feature_flags)


# Make the helpers reachable for tests without importing the
# private leading-underscore symbols. The handler functions stay
# private because they're driven through FastMCP's resource layer.
__all__ = [
    "register",
]


# Auto-cast helper: keep mypy quiet about ``Any`` returns in the
# resource bodies (FastMCP's resource decorator types ``fn`` as
# ``Callable[..., str | bytes | dict | list]``). Cast at the call
# site rather than wrapping every helper in a string-only signature.
cast  # noqa: B018 - re-exported only to keep ``cast`` imported
